from pathlib import Path
import asyncio
import os
import xml.etree.ElementTree as ET
from datetime import datetime
import logging
import aiohttp
from shapely.geometry import Polygon, MultiPolygon
from shapely.wkt import dumps as wkt_dumps
import asyncpg
from dotenv import load_dotenv
from tqdm import tqdm
import psutil
import time
import backoff
from aiohttp import ClientError, ClientTimeout

from ...base import Source, clean_value

logger = logging.getLogger(__name__)

class Cadastral(Source):
    def __init__(self, config):
        super().__init__(config)
        self.field_mapping = {
            'BFEnummer': ('bfe_number', int),
            'forretningshaendelse': ('business_event', str),
            'forretningsproces': ('business_process', str),
            'senesteSagLokalId': ('latest_case_id', str),
            'id_lokalId': ('id_local', str),
            'id_namespace': ('id_namespace', str),
            'registreringFra': ('registration_from', lambda x: datetime.fromisoformat(x.replace('Z', '+00:00'))),
            'virkningFra': ('effect_from', lambda x: datetime.fromisoformat(x.replace('Z', '+00:00'))),
            'virkningsaktoer': ('authority', str),
            'arbejderbolig': ('is_worker_housing', lambda x: x.lower() == 'true'),
            'erFaelleslod': ('is_common_lot', lambda x: x.lower() == 'true'),
            'hovedejendomOpdeltIEjerlejligheder': ('has_owner_apartments', lambda x: x.lower() == 'true'),
            'udskiltVej': ('is_separated_road', lambda x: x.lower() == 'true'),
            'landbrugsnotering': ('agricultural_notation', str)
        }
        
        load_dotenv()
        self.username = os.getenv('DATAFORDELER_USERNAME')
        self.password = os.getenv('DATAFORDELER_PASSWORD')
        if not self.username or not self.password:
            raise ValueError("Missing DATAFORDELER_USERNAME or DATAFORDELER_PASSWORD")
        
        self.page_size = int(os.getenv('CADASTRAL_PAGE_SIZE', '1000'))
        self.batch_size = int(os.getenv('CADASTRAL_BATCH_SIZE', '5000'))
        self.max_concurrent = int(os.getenv('CADASTRAL_MAX_CONCURRENT', '5'))
        self.request_timeout = int(os.getenv('CADASTRAL_REQUEST_TIMEOUT', '300'))
        self.total_timeout = int(os.getenv('CADASTRAL_TOTAL_TIMEOUT', '7200'))
        self.requests_per_second = int(os.getenv('CADASTRAL_REQUESTS_PER_SECOND', '2'))
        self.last_request_time = {}
        self.request_semaphore = asyncio.Semaphore(self.max_concurrent)
        
        self.request_timeout_config = aiohttp.ClientTimeout(
            total=self.request_timeout,
            connect=60,
            sock_read=300
        )
        
        self.total_timeout_config = aiohttp.ClientTimeout(
            total=self.total_timeout,
            connect=60,
            sock_read=300
        )
        
        self.timeout = aiohttp.ClientTimeout(total=self.request_timeout)
        
        self.namespaces = {
            'wfs': 'http://www.opengis.net/wfs/2.0',
            'mat': 'http://data.gov.dk/schemas/matrikel/1',
            'gml': 'http://www.opengis.net/gml/3.2'
        }

    def _get_base_params(self):
        """Get base WFS request parameters without pagination"""
        return {
            'username': self.username,
            'password': self.password,
            'SERVICE': 'WFS',
            'REQUEST': 'GetFeature',
            'VERSION': '2.0.0',
            'TYPENAMES': 'mat:SamletFastEjendom_Gaeldende',
            'SRSNAME': 'EPSG:25832'
        }

    def _get_params(self, start_index=0):
        """Get WFS request parameters with pagination"""
        params = self._get_base_params()
        params.update({
            'startIndex': str(start_index),
            'count': str(self.page_size)
        })
        return params

    def _parse_geometry(self, geom_elem):
        """Parse GML geometry to WKT"""
        try:
            pos_lists = geom_elem.findall('.//gml:posList', self.namespaces)
            if not pos_lists:
                return None

            polygons = []
            for pos_list in pos_lists:
                if not pos_list.text:
                    continue

                coords = [float(x) for x in pos_list.text.strip().split()]
                pairs = [(coords[i], coords[i+1]) 
                        for i in range(0, len(coords), 3)]

                if len(pairs) < 4:
                    continue

                try:
                    polygon = Polygon(pairs)
                    if polygon.is_valid:
                        polygons.append(polygon)
                except Exception as e:
                    logger.warning(f"Error creating polygon: {str(e)}")
                    continue

            if not polygons:
                return None

            final_geom = MultiPolygon(polygons) if len(polygons) > 1 else polygons[0]
            return wkt_dumps(final_geom)

        except Exception as e:
            logger.error(f"Error parsing geometry: {str(e)}")
            return None

    def _parse_feature(self, feature_elem):
        """Parse a single feature"""
        try:
            feature = {}
            
            # Parse all mapped fields
            for xml_field, (db_field, converter) in self.field_mapping.items():
                elem = feature_elem.find(f'.//mat:{xml_field}', self.namespaces)
                if elem is not None and elem.text:
                    try:
                        value = clean_value(elem.text)
                        if value is not None:
                            feature[db_field] = converter(value)
                    except (ValueError, TypeError) as e:
                        logger.warning(f"Error converting field {xml_field}: {str(e)}")
                        continue

            # Parse geometry
            geom_elem = feature_elem.find('.//mat:geometri/gml:MultiSurface', self.namespaces)
            if geom_elem is not None:
                geometry_wkt = self._parse_geometry(geom_elem)
                if geometry_wkt:
                    feature['geometry'] = geometry_wkt

            return feature if feature.get('bfe_number') and feature.get('geometry') else None

        except Exception as e:
            logger.error(f"Error parsing feature: {str(e)}")
            return None

    async def _get_total_count(self, session):
        """Get total number of features from first page metadata"""
        params = {
            'username': self.username,
            'password': self.password,
            'SERVICE': 'WFS',
            'REQUEST': 'GetFeature',
            'VERSION': '2.0.0',
            'TYPENAMES': 'mat:SamletFastEjendom_Gaeldende',
            'SRSNAME': 'EPSG:25832',
            'startIndex': '0',
            'count': '1'  # Just get one feature to check metadata
        }
        
        try:
            logger.info("Getting total count from first page metadata...")
            async with session.get(self.config['url'], params=params) as response:
                response.raise_for_status()
                text = await response.text()
                root = ET.fromstring(text)
                total_available = int(root.get('numberMatched', '0'))
                logger.info(f"Total available features: {total_available:,}")
                return total_available
                
        except Exception as e:
            logger.error(f"Error getting total count: {str(e)}")
            raise

    async def _wait_for_rate_limit(self):
        """Ensure we don't exceed requests_per_second"""
        worker_id = id(asyncio.current_task())
        if worker_id in self.last_request_time:
            elapsed = time.time() - self.last_request_time[worker_id]
            if elapsed < 1.0 / self.requests_per_second:
                await asyncio.sleep(1.0 / self.requests_per_second - elapsed)
        self.last_request_time[worker_id] = time.time()

    @backoff.on_exception(
        backoff.expo,
        (ClientError, asyncio.TimeoutError),
        max_tries=3,
        max_time=60
    )
    async def _fetch_chunk(self, session, start_index, timeout=None):
        """Fetch a chunk of features with rate limiting and retries"""
        async with self.request_semaphore:
            await self._wait_for_rate_limit()
            
            params = self._get_params(start_index)
            
            try:
                logger.info(f"Fetching chunk at index {start_index}")
                async with session.get(
                    self.config['url'], 
                    params=params,
                    timeout=timeout or self.request_timeout_config
                ) as response:
                    if response.status == 429:  # Too Many Requests
                        retry_after = int(response.headers.get('Retry-After', 5))
                        logger.warning(f"Rate limited, waiting {retry_after} seconds")
                        await asyncio.sleep(retry_after)
                        raise ClientError("Rate limited")
                    
                    response.raise_for_status()
                    content = await response.text()
                    root = ET.fromstring(content)
                    
                    # Log response metadata
                    number_matched = root.get('numberMatched', '0')
                    number_returned = root.get('numberReturned', '0')
                    logger.info(f"Chunk {start_index}: matched={number_matched}, returned={number_returned}")
                    
                    features = []
                    for feature_elem in root.findall('.//mat:SamletFastEjendom_Gaeldende', self.namespaces):
                        feature = self._parse_feature(feature_elem)
                        if feature:
                            features.append(feature)
                    
                    logger.info(f"Chunk {start_index}: parsed {len(features)} valid features")
                    return features
                    
            except Exception as e:
                logger.error(f"Error fetching chunk at index {start_index}: {str(e)}")
                raise

    async def _create_tables(self, conn):
        """Create necessary database tables"""
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS cadastral_properties (
                bfe_number INTEGER PRIMARY KEY,
                business_event TEXT,
                business_process TEXT,
                latest_case_id TEXT,
                id_local TEXT,
                id_namespace TEXT,
                registration_from TIMESTAMP WITH TIME ZONE,
                effect_from TIMESTAMP WITH TIME ZONE,
                authority TEXT,
                is_worker_housing BOOLEAN,
                is_common_lot BOOLEAN,
                has_owner_apartments BOOLEAN,
                is_separated_road BOOLEAN,
                agricultural_notation TEXT,
                geometry GEOMETRY(MULTIPOLYGON, 25832)
            );
            
            CREATE INDEX IF NOT EXISTS cadastral_properties_geometry_idx 
            ON cadastral_properties USING GIST (geometry);
        """)

    async def _insert_batch(self, conn, features):
        """Insert a batch of features"""
        if not features:
            return 0
            
        try:
            # Prepare values for insertion
            values = []
            for f in features:
                values.append((
                    f.get('bfe_number'),
                    f.get('business_event'),
                    f.get('business_process'),
                    f.get('latest_case_id'),
                    f.get('id_local'),
                    f.get('id_namespace'),
                    f.get('registration_from'),
                    f.get('effect_from'),
                    f.get('authority'),
                    f.get('is_worker_housing'),
                    f.get('is_common_lot'),
                    f.get('has_owner_apartments'),
                    f.get('is_separated_road'),
                    f.get('agricultural_notation'),
                    f.get('geometry')
                ))

            # Insert with conflict handling
            result = await conn.executemany("""
                INSERT INTO cadastral_properties (
                    bfe_number, business_event, business_process, latest_case_id,
                    id_local, id_namespace, registration_from, effect_from,
                    authority, is_worker_housing, is_common_lot, has_owner_apartments,
                    is_separated_road, agricultural_notation, geometry
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, 
                    ST_GeomFromText($15, 25832))
                ON CONFLICT (bfe_number) DO UPDATE SET
                    business_event = EXCLUDED.business_event,
                    business_process = EXCLUDED.business_process,
                    latest_case_id = EXCLUDED.latest_case_id,
                    id_local = EXCLUDED.id_local,
                    id_namespace = EXCLUDED.id_namespace,
                    registration_from = EXCLUDED.registration_from,
                    effect_from = EXCLUDED.effect_from,
                    authority = EXCLUDED.authority,
                    is_worker_housing = EXCLUDED.is_worker_housing,
                    is_common_lot = EXCLUDED.is_common_lot,
                    has_owner_apartments = EXCLUDED.has_owner_apartments,
                    is_separated_road = EXCLUDED.is_separated_road,
                    agricultural_notation = EXCLUDED.agricultural_notation,
                    geometry = EXCLUDED.geometry
                WHERE EXCLUDED.registration_from >= cadastral_properties.registration_from 
                    OR cadastral_properties.registration_from IS NULL
            """, values)
            
            return len(values)
            
        except Exception as e:
            logger.error(f"Error inserting batch: {str(e)}")
            raise

    async def sync(self, conn):
        """Sync cadastral data"""
        logger.info("Starting cadastral sync...")
        start_time = datetime.now()
        
        # Add memory monitoring
        import psutil
        process = psutil.Process()
        initial_memory = process.memory_info().rss / 1024 / 1024
        logger.info(f"Initial memory usage: {initial_memory:.2f} MB")
        
        # Create tables if they don't exist
        await self._create_tables(conn)
        
        # Initialize queues and events
        feature_queue = asyncio.Queue(maxsize=self.max_concurrent * 2)
        db_queue = asyncio.Queue(maxsize=self.max_concurrent * 2)
        fetch_complete = asyncio.Event()
        processing_complete = asyncio.Event()
        
        # Track progress
        processed_chunks = set()
        total_processed = 0
        
        async def db_worker():
            """Database worker to handle batch inserts"""
            try:
                batch = []
                while True:
                    if db_queue.empty() and processing_complete.is_set():
                        if batch:  # Process any remaining features
                            inserted = await self._insert_batch(conn, batch)
                            logger.info(f"Final batch inserted: {inserted} features")
                        break

                    try:
                        features = await asyncio.wait_for(db_queue.get(), timeout=1.0)
                        batch.extend(features)
                        
                        if len(batch) >= self.batch_size:
                            inserted = await self._insert_batch(conn, batch)
                            logger.info(f"Batch inserted: {inserted} features")
                            batch = []
                            
                        db_queue.task_done()
                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        logger.error(f"Error in db worker: {str(e)}")
                        raise

            except Exception as e:
                logger.error(f"Database worker failed: {str(e)}")
                raise

        async def process_worker():
            """Process features from queue"""
            try:
                features_batch = []
                while True:
                    if feature_queue.empty() and fetch_complete.is_set():
                        if features_batch:
                            await db_queue.put(features_batch)
                        break

                    try:
                        chunk_idx, features = await asyncio.wait_for(
                            feature_queue.get(), timeout=1.0
                        )
                        
                        if chunk_idx in processed_chunks:
                            continue
                            
                        features_batch.extend(features)
                        processed_chunks.add(chunk_idx)
                        
                        if len(features_batch) >= self.batch_size:
                            await db_queue.put(features_batch)
                            features_batch = []
                            
                        feature_queue.task_done()
                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        logger.error(f"Error in process worker: {str(e)}")
                        raise

                processing_complete.set()
            except Exception as e:
                logger.error(f"Process worker failed: {str(e)}")
                raise

        async def fetch_worker():
            """Fetch features from WFS service"""
            try:
                async with aiohttp.ClientSession(timeout=self.total_timeout_config) as session:
                    # Get total count using first page metadata
                    params = {
                        'username': self.username,
                        'password': self.password,
                        'SERVICE': 'WFS',
                        'REQUEST': 'GetFeature',
                        'VERSION': '2.0.0',
                        'TYPENAMES': 'mat:SamletFastEjendom_Gaeldende',
                        'SRSNAME': 'EPSG:25832',
                        'startIndex': '0',
                        'count': '1'
                    }
                    
                    logger.info("Getting total count from first page metadata...")
                    async with session.get(self.config['url'], params=params) as response:
                        response.raise_for_status()
                        text = await response.text()
                        root = ET.fromstring(text)
                        total_features = int(root.get('numberMatched', '0'))
                        logger.info(f"Total available features: {total_features:,}")

                    # Create progress bar
                    pbar = tqdm(total=total_features, desc="Fetching features")
                    
                    # Process all chunks
                    tasks = []
                    for start_index in range(0, total_features, self.page_size):
                        task = asyncio.create_task(
                            self._fetch_chunk(session, start_index)
                        )
                        tasks.append((start_index, task))

                    # Process chunks as they complete
                    for start_index, task in tasks:
                        try:
                            features = await task
                            if features:  # Only process if we got features
                                await feature_queue.put((start_index, features))
                                pbar.update(len(features))
                        except Exception as e:
                            logger.error(f"Error fetching chunk {start_index}: {str(e)}")
                            continue

                    pbar.close()
                    fetch_complete.set()
                    
            except Exception as e:
                logger.error(f"Fetch worker failed: {str(e)}")
                raise

        try:
            # Start workers
            workers = [
                asyncio.create_task(fetch_worker()),
                asyncio.create_task(process_worker()),
                asyncio.create_task(db_worker())
            ]

            # Wait for all workers to complete
            await asyncio.gather(*workers)
            
            # Get final count
            total_count = await conn.fetchval(
                "SELECT COUNT(*) FROM cadastral_properties"
            )
            logger.info(f"Sync completed. Total records in database: {total_count:,}")
            
            # Log total runtime
            end_time = datetime.now()
            duration = end_time - start_time
            logger.info(f"Total runtime: {duration}")
            
            return total_count

        except Exception as e:
            logger.error(f"Sync failed: {str(e)}")
            raise

    async def fetch(self):
        """Not implemented - using sync() directly"""
        raise NotImplementedError("This source uses sync() directly")
