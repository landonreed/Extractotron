#!/usr/bin/env python
from sys import argv
from os import mkdir, remove
from subprocess import Popen
from os.path import join, exists, dirname, abspath, basename
from math import sqrt
from glob import glob
from sh import curl

import logging

from numpy import array
from scipy.cluster.vq import kmeans2

mercator = '+proj=merc +a=6378137 +b=6378137 +lat_ts=0.0 +lon_0=0.0 +x_0=0.0 +y_0=0 +k=1.0 +units=m +nadgrids=@null +no_defs'

def open_logs(name_base):
    ''' Open a pair of logfiles and return a dictionary for subprocess.Popen().
    '''
    return dict(stdout = open(name_base + '.out', 'w'), 
                stderr = open(name_base + '.err', 'w'))

def group_cities(cities):
    ''' Cluster cities into sqrt(len) groups by k-means in unprojected space.
    
        Returns a list of dictionaries, each with a 'cities' list and
        overall bounding box under 'top', 'left', bottom', 'right'.
    '''
    lats = [city['top']/2 + city['bottom']/2 for city in cities]
    lons = [city['left']/2 + city['right']/2 for city in cities]
    
    data = array(zip(lons, lats))
    k = int(round(sqrt(len(cities))))
    
    centers, labels = kmeans2(data, k, iter=100, minit='points')
    groups = [dict(cities=[], lats=[], lons=[]) for i in range(k)]
    
    for (index, label) in enumerate(labels):
        group, city = groups[label], cities[index]

        group['lats'] += (city['top'], city['bottom'])
        group['lons'] += (city['left'], city['right'])
        group['cities'].append(city)
    
    for group in groups:
        group['top'] = max(group['lats'])
        group['left'] = min(group['lons'])
        group['bottom'] = min(group['lats'])
        group['right'] = max(group['lons'])

        del group['lats'], group['lons']
    
    return groups

def osmosis_command(planet_path, cities):
    ''' Generate a complete osmosis command for use with subprocess.Popen().
    '''
    groups = group_cities(cities)
    
    log = open(join(dirname(planet_path), 'logs/osmosis.cmd'), 'w')
    
    osmosis = [
        'osmosis', '--rb', planet_path, '--lp', 'interval=60',
        '--tee', 'outputCount=%d' % len(groups)
        ]
    
    print >> log, ' '.join(osmosis)
    
    for group in groups:
    
        osmosis += [
            '--bb', 'top=%(top).4f' % group, 'left=%(left).4f' % group,
            'bottom=%(bottom).4f' % group, 'right=%(right).4f' % group,
            '--b', '--tee', 'outputCount=%d' % len(group['cities'])
            ]
    
        print >> log, ' ', ' '.join(osmosis[-8:])
        
        for city in group['cities']:
            osmosis += [
                '--bb', 'top=%(top).4f' % city, 'left=%(left).4f' % city,
                'bottom=%(bottom).4f' % city, 'right=%(right).4f' % city,
                '--tee', 'outputCount=2',
                '--wx', city['osm_path'],
                '--wb', city['pbf_path']
                ]
        
            print >> log, '   ', ' '.join(osmosis[-11:-4])
            print >> log, '   ', ' '.join(osmosis[-4:])
    
    log.close()
    
    return osmosis

def extract_cities(planet_path, cities):
    ''' Process planet file through osmosis and output a file for each city.
    '''
    logging.info('Extracting %d cities from %s' % (len(cities), basename(planet_path)))
    logs = open_logs(join(dirname(planet_path), 'logs/osmosis'))
    
    osmosis = Popen(osmosis_command(planet_path, cities), **logs)
    osmosis.wait()
    
    logs['stdout'].close()
    logs['stderr'].close()

def process_coastline(planet_path):
    ''' Process planet file through osmcoastline and output a zipped shapefile.
    '''
    coast_planet_path = join(dirname(planet_path), 'coastline.osm.pbf')
    coast_sqlite_path = join(dirname(planet_path), 'coastline.db')
    coast_shape_base = join(dirname(planet_path), 'land-polygons')
    coast_shape_path = coast_shape_base + '.shp'
    coast_zip_path = coast_shape_base + '.zip'
    
    logging.info('Processing coastline from %s to %s' % (basename(planet_path), basename(coast_sqlite_path)))
    logs = open_logs(join(dirname(planet_path), 'logs/process-coastline'))
    
    if exists(coast_planet_path):
        remove(coast_planet_path)
    
    if exists(coast_sqlite_path):
        remove(coast_sqlite_path)
    
    for extension in ('.zip', '.shp', '.shx', '.prj', '.dbf'):
        if exists(coast_shape_base + extension):
            remove(coast_shape_base + extension)
    
    #
    # Filter complete planet down to only natural=coastline ways.
    #
    osmcoastline_filter = Popen(['osmcoastline_filter', '-o', coast_planet_path, planet_path], **logs)
    osmcoastline_filter.wait()
    
    #
    # Generate coastline sqlite database, creating land + water polygons,
    # coastal rings, and skipping spatial index.
    #
    osmcoastline = Popen('osmcoastline -p both -r -v -i -o'.split() + [coast_sqlite_path, coast_planet_path], **logs)
    osmcoastline.wait()
    
    logging.info('Extracting shapefiles from %s to %s' % (basename(coast_sqlite_path), basename(coast_zip_path)))
    
    #
    # Extract land polygons to mercator-projected shapefiles.
    #
    ogr2ogr = Popen(['ogr2ogr', '-t_srs', mercator, coast_shape_path, coast_sqlite_path, 'land_polygons'], **logs)
    ogr2ogr.wait()
    
    #
    # Archive shapefiles from previous step into a zip file.
    #
    zip = Popen(['zip', '-j', coast_zip_path] + [coast_shape_base + ext for ext in ('.shp', '.shx', '.prj', '.dbf')], **logs)
    zip.wait()
    
    for extension in ('.shp', '.shx', '.prj', '.dbf'):
        if exists(coast_shape_base + extension):
            remove(coast_shape_base + extension)
    
    logs['stdout'].close()
    logs['stderr'].close()

def process_city_osm2pgsql(osm_path, slug, osm2pgsql_style_path):
    ''' Pass extracted OSM data through osm2pgsql to create shapefile archive.
    '''
    prefix = '%s_osm' % slug.replace('-', '_')
    zip_path = join(dirname(osm_path), '%s.osm2pgsql-shps.zip' % slug)
    
    logging.info('Converting from from %s to %s' % (basename(osm_path), basename(zip_path)))
    logs = open_logs(join(dirname(planet_path), 'logs/process-osm2pgsql-%s' % slug))
    
    if exists(zip_path):
        remove(zip_path)
    
    #
    # Import city extract to PostGIS, in unprojected utf-8 slim mode.
    # Clobber existing tables, if any exist.
    #
    osm2pgsql = Popen('osm2pgsql -sluc -C 1024 -i work -U osm -d osm'.split()
                      + ['-S', osm2pgsql_style_path, '-p', prefix, osm_path],
                      **logs)

    osm2pgsql.wait()
    
    filenames = []
    
    for geomtype in ('point', 'polygon', 'line'):
        table_name = '%(prefix)s_%(geomtype)s' % locals()
        shape_base = join(dirname(osm_path), '%(slug)s.osm-%(geomtype)s' % locals())
        shape_path = shape_base + '.shp'
        
        for extension in ('.shp', '.shx', '.prj', '.dbf'):
            if exists(shape_base + extension):
                remove(shape_base + extension)

        #
        # Extract PostGIS tables to shapefiles by geometry type.
        #
        pgsql2shp = Popen('pgsql2shp -rk -u osm -f'.split() + [shape_path, 'osm', table_name], **logs)
        pgsql2shp.wait()
        
        filenames += glob(shape_base + '.???')
    
    #
    # Archive shapefiles from previous steps into a zip file.
    #
    zip = Popen(['zip', '-j', zip_path] + filenames, **logs)
    zip.wait()
    
    for filename in filenames:
        remove(filename)
    
    #
    # Drop city tables from PostGIS.
    #
    for suffix in ('line', 'nodes', 'point', 'polygon', 'rels', 'roads', 'ways'):
        psql = Popen(['psql', '-c', 'DROP TABLE %(prefix)s_%(suffix)s' % locals(), '-U', 'osm', 'osm'], **logs)
        psql.wait()
    
    logs['stdout'].close()
    logs['stderr'].close()

# a small, default list of cities

cities = [
    dict(slug='west-oak', name='West Oakland', top=37.8325, left=-122.3443, bottom=37.7865, right=-122.2586),
    dict(slug='core-sf', name='Core San Francisco', top=37.8097, left=-122.4278, bottom=37.7617, right=-122.3842),
    dict(slug='berkeley', name='U.C. Berkeley', top=37.8810, left=-122.2752, bottom=37.8615, right=-122.2352),
    dict(slug='san-bruno', name='San Bruno', top=37.6457, left=-122.4666, bottom=37.6003, right=-122.2352),
    dict(slug='santa-clara', name='Santa Clara', top=37.3796, left=-121.9992, bottom=37.3215, right=-121.9110),
    dict(slug='san-jose', name='San Jose', top=37.3660, left=-121.9439, bottom=37.3125, right=-121.8485),
    ]

if __name__ == '__main__':

    logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')

    (url, dir) = argv[1:]
    dir += '/stuff'
    
    for newdir in (dir, dir + '/logs'):
        try:
            mkdir(newdir)
        except OSError:
            if not exists(newdir):
                raise
    
    #
    # Download planet.
    #
    planet_path = abspath(join(dir, 'planet.osm.pbf'))
    logging.info('Downloading %s to %s' % (url, basename(planet_path)))
    
    curl(url, o=planet_path)
    
    #
    # Process planet.
    #
    for city in cities:
        city['osm_path'] = join(dirname(planet_path), '%(slug)s.osm.bz2' % city)
        city['pbf_path'] = join(dirname(planet_path), '%(slug)s.osm.pbf' % city)
    
    process_coastline(planet_path)
    extract_cities(planet_path, cities)
    
    osm2pgsql_style_path = join(dirname(__file__), 'postgis/osm2pgsql.style')
    
    for city in cities:
        process_city_osm2pgsql(city['osm_path'], city['slug'], osm2pgsql_style_path)
