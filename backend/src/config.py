SOURCES = {
    "agricultural_fields": {
        "name": "Danish Agricultural Fields",
        "type": "arcgis",
        "description": "Weekly updated agricultural field data",
        "url": "https://kort.vd.dk/server/rest/services/Grunddata/Marker_og_Markblokke/MapServer/12/query",
        "frequency": "weekly",
        "enabled": True,
        "bucket": "landbrugsdata-raw-data"
    },
    "wetlands": {
        "name": "Danish Wetlands Map",
        "type": "wfs",
        "description": "Wetland areas from Danish EPA",
        "url": "https://wfs2-miljoegis.mim.dk/natur/wfs",
        "layer": "natur:kulstof2022",
        "frequency": "static",
        "enabled": True,
        "bucket": "landbrugsdata-raw-data"
    },
    "cadastral": {
        "name": "Danish Cadastral Properties",
        "type": "wfs",
        "description": "Current real estate property boundaries",
        "url": "https://wfs.datafordeler.dk/MATRIKLEN2/MatGaeldendeOgForeloebigWFS/1.0.0/WFS",
        "frequency": "weekly",
        "enabled": True,
        "bucket": "landbrugsdata-raw-data"
    },
    "water_projects": {
        "name": "Danish Water Projects",
        "type": "wfs",
        "description": "Water projects from various Danish programs",
        "url": "https://geodata.fvm.dk/geoserver/wfs",
        "url_mim": "https://wfs2-miljoegis.mim.dk/vandprojekter/wfs",
        "frequency": "weekly",
        "enabled": True,
        "create_combined": True,
        "combined_timeout": 3600,
        "bucket": "landbrugsdata-raw-data"
    },
    "crops": {
        "name": "Danish Agricultural Crop Codes",
        "type": "static",
        "description": "Reference data for crop codes and compensation categories",
        "frequency": "static",
        "enabled": True,
        "bucket": "landbrugsdata-raw-data"
    }
}
