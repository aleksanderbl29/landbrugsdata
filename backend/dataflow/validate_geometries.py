# backend/dataflow/validate_geometries.py
import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions
import geopandas as gpd
import pandas as pd
from shapely import wkt
import logging

class ValidateGeometriesOptions(PipelineOptions):
    @classmethod
    def _add_argparse_args(cls, parser):
        parser.add_argument('--dataset')
        parser.add_argument('--input_bucket')
        parser.add_argument('--output_bucket')

class ValidateAndOptimize(beam.DoFn):
    def process(self, element):
        dataset = element['dataset']
        gdf = element['data']
        
        logging.info(f"Processing {dataset} with {len(gdf)} features")
        
        # Validate geometries
        invalid_count = 0
        fixed_geoms = []
        
        for idx, geom in enumerate(gdf.geometry):
            if not geom.is_valid:
                invalid_count += 1
                try:
                    fixed_geom = geom.buffer(0)
                    fixed_geoms.append(fixed_geom)
                except Exception as e:
                    logging.error(f"Failed to fix geometry at index {idx}: {e}")
                    fixed_geoms.append(None)
            else:
                fixed_geoms.append(geom)
        
        # Update geometries
        gdf.geometry = fixed_geoms
        
        # Remove rows with null geometries
        original_len = len(gdf)
        gdf = gdf.dropna(subset=['geometry'])
        dropped_count = original_len - len(gdf)
        
        # Optimize numeric columns
        for col in gdf.columns:
            if col != 'geometry':
                if gdf[col].dtype == 'float64':
                    gdf[col] = gdf[col].astype('float32')
                elif gdf[col].dtype == 'int64':
                    gdf[col] = pd.to_numeric(gdf[col], downcast='integer')
        
        stats = {
            'dataset': dataset,
            'total_features': original_len,
            'invalid_geometries': invalid_count,
            'dropped_features': dropped_count,
            'memory_usage_mb': gdf.memory_usage(deep=True).sum() / 1024**2
        }
        
        yield {'dataset': dataset, 'data': gdf, 'stats': stats}

def run(argv=None):
    pipeline_options = ValidateGeometriesOptions(argv)
    options = pipeline_options.view_as(ValidateGeometriesOptions)
    
    def read_dataset(dataset):
        gdf = gpd.read_parquet(
            f'gs://{options.input_bucket}/raw/{dataset}/current.parquet'
        )
        return {'dataset': dataset, 'data': gdf}
    
    def write_outputs(element):
        dataset = element['dataset']
        gdf = element['data']
        stats = element['stats']
        
        # Write optimized GeoParquet
        gdf.to_parquet(
            f'gs://{options.output_bucket}/validated/{dataset}/current.parquet',
            compression='zstd',
            compression_level=3,
            index=False,
            row_group_size=100000
        )
        
        # Write validation stats
        pd.DataFrame([stats]).to_csv(
            f'gs://{options.output_bucket}/validated/{dataset}/validation_stats.csv',
            index=False
        )
    
    with beam.Pipeline(options=pipeline_options) as p:
        (p 
         | 'Create Dataset' >> beam.Create([options.dataset])
         | 'Read Data' >> beam.Map(read_dataset)
         | 'Validate and Optimize' >> beam.ParDo(ValidateAndOptimize())
         | 'Write Results' >> beam.Map(write_outputs)
        )

if __name__ == '__main__':
    logging.getLogger().setLevel(logging.INFO)
    run()