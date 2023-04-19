import json
import pystac
import stackstac
import os
import xarray as xr
import geopandas as gpd
from shapely import Polygon
import matplotlib.pyplot as plt
import pandas as pd
import rioxarray as rio
from rasterio.crs import CRS
import rasterio 
import matplotlib.patches as mpatches
import numpy as np
from scipy.stats import sem
import scipy

## setup utility fns 
def check_orig_files(item):
    
    '''this is a function to check that the pystac catalog was created correctly. if somehow variable geotiff files from different dates get added as if they were collected on the same data it will throw an error 
    '''
    
    file_ls = ['orig_file_dis_az', 'orig_file_dis_mag','orig_file_dis_N_ang','orig_file_dis_r']
    
    dt_ls, ref_date_ls, sec_date_ls = [],[],[]
    for file in file_ls:
        
        var_name_dt = f'{file}_datetime'
        var_name_ref = f'{file}_ref_date'
        var_name_sec = f'{file}_sec_date'
        
        var_name_dt = item.extra_fields[file][22:30]
        var_name_ref = item.extra_fields[file].split('+S1_')[1][:15]
        var_name_sec = item.extra_fields[file].split('+S1_')[1].split('_')[9]
        
        dt_ls.append(var_name_dt)
        ref_date_ls.append(var_name_ref)
        sec_date_ls.append(var_name_sec)
       
    if len(set(dt_ls)) != 1:
           print('issue w dt')
    elif len(set(ref_date_ls)) != 1:
             print('issue with ref date')
             
    elif len(set(sec_date_ls)) != 1:
             print('issue w sec date')
            
def get_footprint(ds, crs = None):
    ''' returns a geopandas geodataframe with the outline of an xarray object.
    xr object must have crs formatted (ie ds.crs returns epsg code)
    '''
    
    left = ds.x.data.min()
    right = ds.x.data.max()
    bottom = ds.y.data.min()
    top = ds.y.data.max()

    bbox = [left, bottom, right, top]
    
    footprint = Polygon([
                [bbox[0], bbox[1]],
                [bbox[0], bbox[3]],
                [bbox[2], bbox[3]],
                [bbox[2], bbox[1]]
                ])
    
    gdf = gpd.GeoDataFrame(index=[0], crs = crs, geometry = [footprint])
    
    return gdf

def cube_process(cube):
    
    cube['img1_dt'] = (('time'), pd.to_datetime(cube.img1_date))
    cube['img2_dt'] = (('time'), pd.to_datetime(cube.img2_date))
    
    cube['img_separation'] = (cube.img1_dt - cube.img2_dt).astype('timedelta64[D]') / np.timedelta64(1,'D')

    cube = cube.drop(['img1_date','img2_date'])
    
    cube = cube.rename({'img1_dt':'img1_date',
                        'img2_dt':'img2_date'})
    return  cube

def calc_sem(x):
    ''' calc standard error of measurement for an xarray data array at a given time step
    '''
    return sem(((x)*365).flatten(), nan_policy='omit')

## fn to combine, clip and build main data object structures 
def clip_glacier_add_dem(rgi_id, rgi_outline_df, retreat_xr, dem_xr, output='full'): #all in local utm
    
    '''workflow to construct an xarray dataset for a single glacier containing velocity data and dem.
    steps are:
    1. clip, 2., expand to dataset along band dim, 3. clip dem, 4. downsample dem to match
    retreat data, 5. add SEM as variable calculated over entire glacier for each time step. 
    6., break up by elevation quartiles 
    '''
    
    
    rgi_single_outline = rgi_outline_df.loc[rgi_outline_df['RGIId'] == rgi_id]
    
    retreat_clip = retreat_xr.rio.clip(rgi_single_outline.geometry, rgi_single_outline.crs)
    print('retreat clipped')
    #convert one of the attrs to str so that it can be saved to netcdf
    retreat_clip.attrs['spec'] = str(retreat_clip.attrs['spec'])
    
    retreat_clip_ds = retreat_clip.to_dataset(dim='band')
    
    valid_pixels = retreat_clip_ds.dis_mag.count(dim=['x','y'])
    valid_pixels_max = retreat_clip_ds.dis_mag.notnull().any('time').sum(['x','y'])
    retreat_clip_ds['cov'] = valid_pixels / valid_pixels_max
    #remove time steps where cov < 0.5
    retreat_clip_ds = retreat_clip_ds.where(retreat_clip_ds.cov >= 0.5, drop=True)
    print('cov done')

    dem_clip = dem_xr.rio.clip(rgi_single_outline.geometry, rgi_single_outline.crs)
    print('dem clipped')
    dem_downsamp = dem_clip.interp_like(retreat_clip_ds, method = 'nearest')
    
    retreat_clip_ds = retreat_clip_ds.drop_dims('band')
    retreat_clip_ds['dis_mag_my'] = retreat_clip_ds['dis_mag']*365
    retreat_clip_ds['z'] = dem_downsamp.NASADEM_HGT
    retreat_clip_ds['sem_mag'] = retreat_clip_ds.dis_mag_my.stack(xy=('x','y')).reduce(scipy.stats.sem, dim='xy', nan_policy='omit')

    print('sem calculated')
    
    
    zmin = np.nanmin(dem_downsamp.NASADEM_HGT.data)
    zq1 = np.nanpercentile(dem_downsamp.NASADEM_HGT.data, 25)
    zmed = np.nanmedian(dem_downsamp.NASADEM_HGT.data)
    zq3 = np.nanpercentile(dem_downsamp.NASADEM_HGT.data, 75)
    zmax = np.nanmax(dem_downsamp.NASADEM_HGT.data)
    
    z0 = dem_downsamp.NASADEM_HGT.where(np.logical_and(dem_downsamp.NASADEM_HGT >= zmin, dem_downsamp.NASADEM_HGT <= zq1), drop=True)
    z1 = dem_downsamp.NASADEM_HGT.where(np.logical_and(dem_downsamp.NASADEM_HGT >= zq1, dem_downsamp.NASADEM_HGT <= zmed), drop=True)
    z2 = dem_downsamp.NASADEM_HGT.where(np.logical_and(dem_downsamp.NASADEM_HGT >= zmed, dem_downsamp.NASADEM_HGT <= zq3), drop=True)
    z3 = dem_downsamp.NASADEM_HGT.where(np.logical_and(dem_downsamp.NASADEM_HGT >= zq3, dem_downsamp.NASADEM_HGT <= zmax), drop=True)
    print('z stuff')
    retreat_clip_ds['z0'] = z0
    retreat_clip_ds['z1'] = z1
    retreat_clip_ds['z2'] = z2
    retreat_clip_ds['z3'] = z3
    
    z0_cond_min = retreat_clip_ds.z0.min().data >= zmin
    z0_cond_max = retreat_clip_ds.z0.max().data < zq1+1
    z1_cond_min = retreat_clip_ds.z1.min().data >= zq1
    z1_cond_max = retreat_clip_ds.z1.max().data <zmed + 1
    z2_cond_min = retreat_clip_ds.z2.min().data >= zmed
    z2_cond_max = retreat_clip_ds.z2.max().data < zq3 + 1
    z3_cond_min = retreat_clip_ds.z3.min().data >= zq3
    z3_cond_max = retreat_clip_ds.z3.max().data < zmax+1
    
    cond_ls = [z0_cond_min, z0_cond_max, z1_cond_min, z1_cond_max,
               z2_cond_min, z2_cond_max, z3_cond_min, z3_cond_max]
    
    test = all(i for i in cond_ls)
    retreat_clip_ds['z0_sem'] = retreat_clip_ds.where(retreat_clip_ds['z0'].notnull(), drop=True).dis_mag_my.stack(xy=('x','y')).reduce(scipy.stats.sem, dim='xy', nan_policy='omit')
    retreat_clip_ds['z1_sem'] = retreat_clip_ds.where(retreat_clip_ds['z1'].notnull(), drop=True).dis_mag_my.stack(xy=('x','y')).reduce(scipy.stats.sem, dim='xy', nan_policy='omit')
    retreat_clip_ds['z2_sem'] = retreat_clip_ds.where(retreat_clip_ds['z2'].notnull(), drop=True).dis_mag_my.stack(xy=('x','y')).reduce(scipy.stats.sem, dim='xy', nan_policy='omit')
    retreat_clip_ds['z3_sem'] = retreat_clip_ds.where(retreat_clip_ds['z3'].notnull(), drop=True).dis_mag_my.stack(xy=('x','y')).reduce(scipy.stats.sem, dim='xy', nan_policy='omit')
    print('finished z stuff')
    
    if test != True:
        
        print('there is an elevation masking issue here')
        
    else:
    
        print('elevation masking should be all good')
        
   
    
    return retreat_clip_ds
    
    #elif output == 'seasonal':
    #    retreat_clip_ds = retreat_clip_ds.where(retreat_clip_ds.img1_date.dt.season == retreat_clip_ds.img2_date.dt.season, drop=True)
    
     #   retreat_gb = retreat_clip_ds.groupby(retreat_clip_ds.time.dt.season).mean()
        #retreat_gb.to_netcdf(f'/uufs/chpc.utah.edu/common/home/cryosphere/emarshall/328_velocity_results/retreat/ds_{rgi_id}.nc')
     #   return retreat_gb
 
    
## functions to create datasets for seasonal analysis
def calc_seasonal_sem_by_z(input_ds, z, var,rgi_id):
    
    gb = input_ds.groupby(input_ds.time.dt.season).mean()
    
    if z == 'full':
        
        winter = gb.sel(season='DJF')['sem_mag'].data
        spring = gb.sel(season='MAM')['sem_mag'].data
        summer = gb.sel(season='JJA')['sem_mag'].data
        fall = gb.sel(season='SON')['sem_mag'].data
    
    else:
        
        z_gb = gb.where(gb[f'{z}'].notnull(), drop=True)
        z_gb['sem_mag'] = (('season'), [calc_sem(z_gb.isel(season=s).dis_mag.data) for s in range(len(z_gb.season))])
        
        winter = z_gb.sel(season='DJF')['sem_mag'].data
        spring = z_gb.sel(season='MAM')['sem_mag'].data
        summer = z_gb.sel(season='JJA')['sem_mag'].data
        fall = z_gb.sel(season='SON')['sem_mag'].data
        
    d = {'RGIId':rgi_id, 'var':var, 'z':z, 'winter': winter,
             'spring':spring, 'summer': summer, 'fall':fall}
            
    df = pd.DataFrame(d, index=[0])
    
    return df
    
        
def calc_seasonal_mean_by_z(input_ds, z, var, rgi_id):
        
    gb = input_ds.groupby(input_ds.time.dt.season).mean()
    
    if z == 'full':

        winter = gb.sel(season='DJF')[f'{var}'].mean(dim=['x','y']).compute().data*365
        spring = gb.sel(season='MAM')[f'{var}'].mean(dim=['x','y']).compute().data*365
        summer = gb.sel(season='JJA')[f'{var}'].mean(dim=['x','y']).compute().data*365
        fall = gb.sel(season='SON')[f'{var}'].mean(dim=['x','y']).compute().data*365

    else:
        z_gb = gb.where(gb[f'{z}'].notnull(), drop=True)

        winter = z_gb.sel(season='DJF')[f'{var}'].mean(dim=['x','y']).compute().data*365
        spring = z_gb.sel(season='MAM')[f'{var}'].mean(dim=['x','y']).compute().data*365
        summer = z_gb.sel(season='JJA')[f'{var}'].mean(dim=['x','y']).compute().data*365
        fall = z_gb.sel(season='SON')[f'{var}'].mean(dim=['x','y']).compute().data*365
    
    d = {'RGIId':rgi_id, 'var': var, 'z':z, 'winter': winter,
             'spring':spring, 'summer': summer, 'fall':fall}
            
    df = pd.DataFrame(d, index=[0])
    
    return df
    

def wrapper_single_glacier(rgi_id, rgi_full, retreat_xr, dem_xr, var):
    '''wraps the above two functions, returns a dataframe with seasonal velocities for each elevation quartile
       input args are: rgi_id (str), full or subset rgi gpdf
       retreat xr object (read from stackstac) in local utm,
       NASADEM xr object projected to local utm 
       variable for which you want seasonal means to be calculated
       
   '''
    ds = clip_glacier_add_dem(rgi_id, rgi_full, retreat_xr, dem_xr)
        
    df_mag = pd.concat([calc_seasonal_mean_by_z(ds, z, var, rgi_id) for z in ['z0','z1','z2','z3','full']])
    df_sem = pd.concat([calc_seasonal_sem_by_z(ds, z, 'sem_mag', rgi_id) for z in ['z0', 'z1', 'z2', 'z3', 'full']])
    
    df = pd.concat([df_mag, df_sem])

    return df

def wrapper_all_glaciers(xr_dict, var):
    
    df_dismag_ls, df_sem_ls = [],[]
    
    for key in xr_dict.keys():
        
        df_dismag = pd.concat([calc_seasonal_mean_by_z(xr_dict[key], z, var, key) for z in ['z0','z1','z2','z3','full']])
        df_dismag_ls.append(df_dismag)
        df_sem = pd.concat([calc_seasonal_sem_by_z(xr_dict[key],z,var, key) for z in ['z0','z1','z2','z3','full']])
        df_sem_ls.append(df_sem)
        
        df_full = pd.concat([df_dismag_ls, df_sem_ls])
        return df_full
        
def read_and_process_to_dict(path_to_file_dir):
    
    files = os.listdir(path_to_file_dir)
    files = [f for f in files if 'rgi' not in f]
    
    keys_ls, vals_ls = [],[]
    
    for f in range(len(files)):
        
        g = files[f][3:-3]
        keys_ls.append(g)
        ds = xr.open_dataset(os.path.join(path_to_file_dir, files[f]))
        ds['image_separation'] = (ds.img_separation*-1)
        ds = ds.where(ds.img1_date.dt.season == ds.img2_date.dt.season, drop=True)
        
        vals_ls.append(ds)
    retreat_dict = dict(zip(keys_ls, vals_ls))
    
    return retreat_dict
        