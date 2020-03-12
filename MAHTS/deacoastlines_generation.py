#!/usr/bin/env python
# coding: utf-8

import os
import sys
import mock
import otps
import datacube
import datetime
import multiprocessing
import numpy as np
import xarray as xr
import pandas as pd
import geopandas as gpd
from affine import Affine
from functools import partial
from shapely.geometry import shape
from datacube.helpers import write_geotiff
from datacube.utils.geometry import GeoBox, Geometry, CRS
from datacube.virtual import catalog_from_file, construct

sys.path.append('../Scripts')
from dea_datahandling import mostcommon_crs
from dea_spatialtools import interpolate_2d
from dea_spatialtools import interpolate_2d_dask

start_time = datetime.datetime.now()


def get_geopoly(index, gdf):
    """
    Selects a row from a geopandas.GeoDataFrame, and converts this
    into a geopolygon feature as an input to dc.load
    """
    return Geometry(geo=gdf.loc[index].geometry.__geo_interface__, 
                    crs=CRS(gdf.crs['init']))


def custom_native_geobox(ds, measurements=None, basis=None):
    """
    Obtains native geobox info from dataset metadata
    """
    geotransform = ds.metadata_doc['grids']['default']['transform']
    shape = ds.metadata_doc['grids']['default']['shape']
    crs = CRS(ds.metadata_doc['crs'])
    affine = Affine(geotransform[0], 0.0, 
                    geotransform[2], 0.0, 
                    geotransform[4], geotransform[5])
    return GeoBox(width=shape[1], height=shape[0], affine=affine, crs=crs)


def load_mndwi(dc, 
               query,
               yaml_path='MAHTS_virtual_products.yaml', 
               product_name='ls_nbart_mndwi',
               virtual_products=True):
    """
    This function uses virtual products to load data from GA Collection 
    3 Landsat 5, 7 and 8, calculate custom remote sensing indices, and 
    return the data as a single xarray.Dataset.
    
    To minimise resampling effects and maintain the highest data 
    fidelity required for subpixel coastline extraction, this workflow 
    applies masking and index calculation at native resolution, and 
    only re-projects to the most common CRS for the query using average 
    resampling in the final step.
    """

    # Identify the most common CRS in the region, so data can be loaded with 
    # minimal distortion. The dictionary comprehension is required as 
    # dc.find_datasets does not work in combination with dask_chnks
    crs = mostcommon_crs(dc=dc, product='ga_ls8c_ard_3', 
                         query={k: v for k, v in query.items() if 
                                k not in ['dask_chunks']})
    
    if virtual_products:
    
        # Load in virtual product catalogue and select MNDWI product
        catalog = catalog_from_file(yaml_path)
        product = catalog[product_name]

        # Construct a new version of the product using most common CRS
        product_reproject = construct(input=product,
                                      reproject={'output_crs': str(crs), 
                                                 'resolution': (-30, 30),
                                                 'align': (15, 15)},          
                                      resampling='average')

        # Determine geobox with custom function to increase lazy loading 
        # speed (will eventually be done automatically within virtual 
        # products)
        with mock.patch('datacube.virtual.impl.native_geobox', 
                        side_effect=custom_native_geobox):
            ds = product_reproject.load(dc, **query)
    
    else:
        
        from dea_datahandling import load_ard
        from dea_bandindices import calculate_indices
        
        ds = load_ard(dc=dc, 
              measurements=['nbart_blue', 'nbart_green', 'nbart_red', 
                            'nbart_nir', 'nbart_swir_1', 'nbart_swir_2'], 
              min_gooddata=0.0,
              products=['ga_ls5t_ard_3', 'ga_ls7e_ard_3', 'ga_ls8c_ard_3'], 
              output_crs=crs,
              resampling={'fmask': 'nearest', 
                          'oa_fmask': 'nearest', 
                          'nbart_contiguity': 'nearest',
                          'oa_nbart_contiguity': 'nearest',
                          '*': 'average'},
              resolution=(-30, 30),  
              gqa_iterative_mean_xy=[0, 1],
              align=(15, 15),
              group_by='solar_day',
              mask_contiguity=False,
              **query)

        ds = (calculate_indices(ds, index=['MNDWI'], 
                                collection='ga_ls_3', 
                                drop=True)
              .rename({'MNDWI': 'mndwi'}))
        
        
    return ds


def model_tides(ds, points_gdf, extent_buffer=0.025):
    """
    Takes an xarray.Dataset (`ds`), extracts a subset of tide modelling 
    points from a geopandas.GeoDataFrame based on`ds`'s extent, then 
    uses the OTPS tidal model to model tide heights for every point
    at every time step in `ds`.
    
    The output is a geopandas.GeoDataFrame with a "time" index 
    (matching the time steps in `ds`), and a "tide_m" column giving the 
    tide heights at each point location.
    """
    
    # Obtain extent of loaded data, and buffer to ensure that tides are
    # modelled reliably and comparably across grid tiles
    ds_extent = shape(ds.geobox.geographic_extent.json)
    buffered = ds_extent.buffer(extent_buffer)
    subset_gdf = points_gdf[points_gdf.geometry.intersects(buffered)]

    # Extract lon, lat from tides, and time from satellite data
    x_vals = subset_gdf.geometry.centroid.x
    y_vals = subset_gdf.geometry.centroid.y
    observed_datetimes = ds.time.data.astype('M8[s]').astype('O').tolist()

    # Create list of lat/lon/time scenarios to model
    observed_timepoints = [otps.TimePoint(lon, lat, date) 
                           for date in observed_datetimes
                           for lon, lat in zip(x_vals, y_vals)]

    # Model tides for each scenario
    observed_predictedtides = otps.predict_tide(observed_timepoints)

    # Output results into pandas.DataFrame
    tidepoints_df = pd.DataFrame([(i.timepoint.timestamp, 
                                   i.timepoint.lon, 
                                   i.timepoint.lat, 
                                   i.tide_m) for i in observed_predictedtides], 
                                 columns=['time', 'lon', 'lat', 'tide_m']) 

    # Convert data to spatial geopandas.GeoDataFrame
    tidepoints_gdf = gpd.GeoDataFrame(data={'time': tidepoints_df.time, 
                                            'tide_m': tidepoints_df.tide_m}, 
                                      geometry=gpd.points_from_xy(tidepoints_df.lon, 
                                                                  tidepoints_df.lat), 
                                      crs={'init': 'EPSG:4326'})

    # Reproject to satellite data CRS
    tidepoints_gdf = tidepoints_gdf.to_crs(epsg=ds.crs.epsg)

    # Fix time and set to index
    tidepoints_gdf['time'] = pd.to_datetime(tidepoints_gdf['time'], utc=True)
    tidepoints_gdf = tidepoints_gdf.set_index('time')
    
    return tidepoints_gdf


def interpolate_tide(timestep_ds, 
                     tidepoints_gdf, 
                     method='rbf', 
                     factor=20, 
                     dask=True):    
    """
    Extract a subset of tide modelling point data for a given time-step,
    then interpolate these tides into the extent of the xarray dataset.
    """  
  
    # Extract subset of observations based on timestamp of imagery
    time_string = str(timestep_ds.time.values)[0:19].replace('T', ' ')
    tidepoints_subset = tidepoints_gdf.loc[time_string]
    print(time_string, end='\r')
    
    # Get lists of x, y and z (tide height) data to interpolate
    x_coords = tidepoints_subset.geometry.x,
    y_coords = tidepoints_subset.geometry.y,
    z_coords = tidepoints_subset.tide_m
    
    if dask:
    
        # Interpolate tides into the extent of the satellite timestep
        out_tide = interpolate_2d_dask(ds=timestep_ds,
                                  x_coords=x_coords,
                                  y_coords=y_coords,
                                  z_coords=z_coords,
                                  method=method,
                                  factor=factor)
        
    else:
        
        # Interpolate tides into the extent of the satellite timestep
        out_tide = interpolate_2d(ds=timestep_ds,
                                  x_coords=x_coords,
                                  y_coords=y_coords,
                                  z_coords=z_coords,
                                  method=method,
                                  factor=factor)
    
    # Return data as a Float32 to conserve memory
    return out_tide.astype(np.float32)


def multiprocess_apply(ds, dim, func):
    """
    Applies a custom function along the dimension of an xarray.Dataset,
    then combines the output to match the original dataset.
    """
    
    pool = multiprocessing.Pool(multiprocessing.cpu_count() - 1)
    print(f'Parallelising {multiprocessing.cpu_count() - 1} processes')
    out_list = pool.map(func, 
                        iterable=[group for (i, group) in ds.groupby(dim)])
    
    # Combine to match the original dataset
    return xr.concat(out_list, dim=ds[dim])


def load_tidal_subset(year_ds, tide_cutoff_min, tide_cutoff_max):
    """
    For a given year of data, thresholds data to keep observations
    within a minimum and maximum tide height cutoff range, and load
    the data into memory.
    """
    
    # Print status
    year = year_ds.time[0].dt.year.item()
    print(f'Processing {year}')
    
    # Determine what pixels were acquired in selected tide range, and 
    # drop time-steps without any relevant pixels to reduce data to load
    tide_bool = ((year_ds.tide_m >= tide_cutoff_min) & 
                 (year_ds.tide_m <= tide_cutoff_max))
    year_ds = year_ds.sel(time=tide_bool.sum(dim=['x', 'y']) > 0)
    
    # Apply mask, and load in corresponding high tide data
    year_ds = year_ds.where(tide_bool)
    return year_ds.compute()

    
def tidal_composite(year_ds, 
                    label, 
                    label_dim, 
                    output_dir, 
                    output_suffix='',
                    export_geotiff=False):
    """
    For a given year of data, takes median, counts and standard 
    deviationo of valid water index results, and optionally writes 
    each water index, tide height, standard deviation and valid pixel 
    counts for the time period to file as GeoTIFFs.
    """
        
    # Compute median water indices and counts of valid pixels
    median_ds = year_ds.median(dim='time', keep_attrs=True)
    median_ds['count'] = (year_ds.mndwi
                          .count(dim='time', keep_attrs=True)
                          .astype('int16'))
    median_ds['stdev'] = year_ds.mndwi.std(dim='time', keep_attrs=True)
    
    # Write each variable to file  
    if export_geotiff:
        for i in median_ds:
            try:
                
                # Write using float nodata type
                geotiff_profile = {'blockxsize': 1024, 
                                   'blockysize': 1024, 
                                   'compress': 'deflate', 
                                   'zlevel': 5,
                                   'nodata': np.nan}
                
                write_geotiff(filename=f'{output_dir}/{str(label)}_{i}{output_suffix}.tif', 
                              dataset=median_ds[[i]],
                              profile_override=geotiff_profile)
            except:
                
                # Update nodata value for int data type
                geotiff_profile.update(nodata=-999)
                write_geotiff(filename=f'{output_dir}/{str(label)}_{i}{output_suffix}.tif', 
                              dataset=median_ds[[i]],
                              profile_override=geotiff_profile)
            
    # Set coordinate and dim
    median_ds = (median_ds
                 .assign_coords(**{label_dim: label})
                 .expand_dims(label_dim)) 
        
    return median_ds


def export_annual_gapfill(ds, 
                          output_dir, 
                          tide_cutoff_min, 
                          tide_cutoff_max):
    """
    To calculate both annual median composites and three-year gapfill
    composites without having to load more than three years in memory 
    at the one time, this function loops through the years in the 
    dataset, progressively updating three datasets (the previous year, 
    current year and subsequent year of data).
    """

    # Create empty vars containing un-composited data from the previous,
    # current and future year. This is progressively updated to ensure that
    # no more than 3 years of data are loaded into memory at any one time
    previous_ds = None
    current_ds = None
    future_ds = None

    # Iterate through each year in the dataset, starting at one year before
    for year in np.unique(ds.time.dt.year) - 1:

        # Load data for the subsequent year
        future_ds = load_tidal_subset(ds.sel(time=str(year + 1)), 
                                      tide_cutoff_min=tide_cutoff_min,
                                      tide_cutoff_max=tide_cutoff_max)

        # If the current year var contains data, combine these observations
        # into median annual high tide composites and export GeoTIFFs
        if current_ds:

            # Generate composite
            tidal_composite(current_ds, 
                            label=year,
                            label_dim='year',
                            output_dir=output_dir, 
                            export_geotiff=True)        

        # If ALL of the previous, current and future year vars contain data,
        # combine these three years of observations into a single median 
        # 3-year gapfill composite
        if previous_ds and current_ds and future_ds:

            # Concatenate the three years into one xarray.Dataset
            gapfill_ds = xr.concat([previous_ds, current_ds, future_ds], 
                                   dim='time')

            # Generate composite
            tidal_composite(gapfill_ds,
                            label=year,
                            label_dim='year',
                            output_dir=output_dir, 
                            output_suffix='_gapfill',
                            export_geotiff=True)        

        # Shift all loaded data back so that we can re-use it in the next
        # iteration and not have to load the same data multiple times
        previous_ds = current_ds
        current_ds = future_ds
        future_ds = []

        

def main(argv=None):

    if argv is None:

        argv = sys.argv
        print(sys.argv)

    # If no user arguments provided
    if len(argv) < 3:

        str_usage = "You must specify a study area ID and name"
        print(str_usage)
        sys.exit()
        
    # Set study area and name for analysis
    study_area = int(argv[1])
    output_name = str(argv[2])
    
    # If output folder doesn't exist, create it
    output_dir = f'output_data/{output_name}_{study_area}'
    os.makedirs(output_dir, exist_ok=True)
    
    # Connect to datacube    
    dc = datacube.Datacube(app='DEACoastLines_generation', env='c3-samples')
    
    # Start local dask client
    from datacube.utils.dask import start_local_dask
    client = start_local_dask(mem_safety_margin='3gb')
    print(client)    

    ###########################
    # Load supplementary data #
    ###########################

    # Tide points are used to model tides across the extent of the satellite data
    points_gdf = gpd.read_file('input_data/tide_points_coastal.geojson')

    # Albers grid cells used to process the analysis
    gridcell_gdf = (gpd.read_file('input_data/50km_albers_grid_clipped.shp')
                .to_crs(epsg=4326)
                .set_index('id'))

    ################
    # Loading data #
    ################
    
    # Create query
    study_area_geopoly = get_geopoly(study_area, gridcell_gdf)
    query = {'geopolygon': study_area_geopoly,
             'time': ('1987', '2019'),
             'cloud_cover': [0, 90],
             'dask_chunks': {'time': 1, 'x': 1000, 'y': 1000}}

    # Load virtual product    
    ds = load_mndwi(dc, query, virtual_products=False)
    
    ###################
    # Tidal modelling #
    ###################
    
    # Model tides at point locations
    tidepoints_gdf = model_tides(ds, points_gdf)

#     # Interpolate tides for each timestep into the spatial extent of the data    
#     interp_tide = partial(interpolate_tide,
#                           tidepoints_gdf=tidepoints_gdf,
#                           factor=50, dask=False)
#     ds['tide_m'] = multiprocess_apply(ds=ds,
#                                               dim='time',
#                                               func=interp_tide)   

    # Interpolate tides for each timestep into the spatial extent of the data     
    interp_tide = partial(interpolate_tide,
                          tidepoints_gdf=tidepoints_gdf,
                          factor=50, dask=True)
    ds['tide_m'] = ds.groupby('time').apply(interp_tide)
    

    # Determine tide cutoff
    tide_cutoff_buff = (
        (ds['tide_m'].max(dim='time') - ds['tide_m'].min(dim='time')) * 0.25)
    tide_cutoff_min = 0.0 - tide_cutoff_buff
    tide_cutoff_max = 0.0 + tide_cutoff_buff
    
    ##############################
    # Generate yearly composites #
    ##############################

    # Iterate through each year and export annual and 3-year gapfill composites
    export_annual_gapfill(ds, 
                          output_dir, 
                          tide_cutoff_min, 
                          tide_cutoff_max)    

    print(f'{(datetime.datetime.now() - start_time).seconds / 60:.1f} minutes')
    
        
if __name__ == "__main__":
    main()