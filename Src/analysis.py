# =====================================================================
# Toronto Paramedic Dispatch
# =====================================================================

import re
from itertools import chain
from datetime import time
from pathlib import Path
import requests
import pandas as pd
import geopandas as gpd
import plotly.express as px
import plotly.graph_objects as go
from shapely.geometry import Polygon, MultiPolygon

# -----------------------
# Paths / config
# -----------------------
DATA_XLSX = Path("data/paramedic-services-incident-data-2024.xlsx")
GEOJSON_FILE = Path("data/Toronto-Ontario.geojson")
OUTPUT_HTML = Path("toronto_paramedic_dispatch_map.html")


# -----------------------
# Load paramedic incidents
# -----------------------
paramedic_data = pd.read_excel(DATA_XLSX, engine="openpyxl")
paramedic_data.columns = [c.lower().strip() for c in paramedic_data.columns]

if 'forward_sortation_area' in paramedic_data.columns:
    paramedic_data = paramedic_data.rename(columns={'forward_sortation_area': 'fsa'})
paramedic_data['fsa'] = paramedic_data['fsa'].astype(str).str.strip().str.upper()

paramedic_data['dispatch_time'] = pd.to_datetime(paramedic_data['dispatch_time'], errors='coerce')
paramedic_data = paramedic_data.dropna(subset=['dispatch_time', 'fsa'])

paramedic_data['incident_type'] = paramedic_data.get('incident_type', '').replace('-', 'N/A')
paramedic_data['fsa'] = paramedic_data['fsa'].replace('-', 'N/A')
paramedic_data = paramedic_data[paramedic_data['fsa'].str.match(r'^M', na=False)].copy()

# -----------------------
# Time-based enrichments
# -----------------------
month_order = ["January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]
paramedic_data['month'] = pd.Categorical(
    paramedic_data['dispatch_time'].dt.month.map({i + 1: m for i, m in enumerate(month_order)}),
    categories=month_order,
    ordered=True
)
day_name_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
paramedic_data['day_name'] = pd.Categorical(
    paramedic_data['dispatch_time'].dt.day_name(),
    categories=day_name_order,
    ordered=True
)
paramedic_data['time'] = paramedic_data['dispatch_time'].dt.time
paramedic_data['day_time'] = paramedic_data['time'].apply(lambda x: "am" if x < time(12, 0) else "pm")


def day_time_bucket(a_time):
    if time(6, 0) <= a_time < time(12, 0):
        return "Morning"
    elif time(12, 0) <= a_time < time(18, 0):
        return "Afternoon"
    elif time(18, 0) <= a_time <= time(23, 59):
        return "Evening"
    else:
        return "Night"


paramedic_data['time_category'] = paramedic_data['time'].apply(day_time_bucket)

priority_map = {1: "Delta", 3: "Charlie", 4: "Bravo", 5: "Alpha", 9: "Echo",
                11: "Alpha1", 12: "Alpha2", 13: "Alpha3", 14: "Code2"}
paramedic_data['priority_category'] = paramedic_data.get('priority_number').map(priority_map)

# -----------------------
# Fetch Toronto FSA -> borough/neighborhood mapping
# -----------------------
wiki_url = "https://en.wikipedia.org/wiki/List_of_postal_codes_of_Canada:_M"
wiki_df = pd.read_html(requests.get(wiki_url, headers={"User-Agent": "Mozilla/5.0"}).text, header=0)[0]
wiki_df.columns = [c.strip() for c in wiki_df.columns]

flat = [v.strip() for col in wiki_df.columns for v in wiki_df[col].dropna().astype(str) if "Not assigned" not in v]
pattern = re.compile(r'^([A-Za-z]\d[A-Za-z])\s+([^()]+?)(?:\s+(\(.+\)))?$')
toronto_rows = [{'FSA': m.group(1).upper().strip(),
                 'borough': m.group(2).strip(),
                 'neighborhood': (m.group(3) or "").replace("(", "").replace(")", "").strip()}
                for s in flat if (m := pattern.match(s))]

toronto_fsa_names = pd.DataFrame(toronto_rows).drop_duplicates('FSA')

# -----------------------
# Dispatch counts per FSA
# -----------------------
total_number_per_region = paramedic_data.groupby('fsa').size().reset_index(name='total_count')
total_number_per_region['FSA'] = total_number_per_region['fsa'].astype(str).str.upper()

# -----------------------
# Load GeoJSON and merge
# -----------------------
ontario_gdf = gpd.read_file(GEOJSON_FILE)
if 'CFSAUID' in ontario_gdf.columns: ontario_gdf = ontario_gdf.rename(columns={'CFSAUID': 'FSA'})
ontario_gdf['FSA'] = ontario_gdf['FSA'].astype(str).str.upper()

gdf = ontario_gdf.merge(total_number_per_region[['FSA', 'total_count']], on='FSA', how='left')
gdf['total_count'] = gdf['total_count'].fillna(0).astype(int)
gdf = gdf[gdf['FSA'].str.startswith('M')].merge(
    toronto_fsa_names, on='FSA', how='left'
).fillna("Not assigned")

# -----------------------
# Compute centroids
# -----------------------
gdf_proj = gdf.to_crs(epsg=3857)
gdf_proj['centroid_geom'] = gdf_proj.geometry.centroid
centroids_wgs84 = gdf_proj.set_geometry('centroid_geom').to_crs(epsg=4326)
gdf['centroid_lon'] = centroids_wgs84.geometry.x.values
gdf['centroid_lat'] = centroids_wgs84.geometry.y.values


# -----------------------
# Fix geometries
# -----------------------
def fix_geometry(geom):
    return geom if isinstance(geom, (Polygon, MultiPolygon)) else None


gdf['geometry'] = gdf['geometry'].apply(fix_geometry)

# -----------------------
# Plot choropleth
# -----------------------
fig = px.choropleth_mapbox(
    gdf, geojson=gdf.__geo_interface__, locations='FSA', featureidkey='properties.FSA',
    color='total_count', color_continuous_scale='YlOrRd',
    range_color=[0, gdf['total_count'].max()],
    mapbox_style='carto-positron', center={'lat': 43.7, 'lon': -79.4},
    zoom=10, opacity=0.6, custom_data=['borough', 'neighborhood', 'total_count']
)
fig.update_traces(
    hovertemplate="<b>FSA: %{location}</b><br>Borough: %{customdata[0]}<br>Neighborhood: %{customdata[1]}<br>Dispatches: %{customdata[2]}<extra></extra>")
fig.add_trace(go.Scattermapbox(
    lon=gdf['centroid_lon'], lat=gdf['centroid_lat'], mode='text', text=gdf['FSA'],
    textfont=dict(size=10, color='black'), showlegend=False, hoverinfo='skip'
))
fig.update_layout(
    title={
        'text': "Toronto Paramedic Dispatch Count",
        'y': 0.97,
        'x': 0.5,
        'xanchor': 'center',
        'yanchor': 'top',
        'font': {
            'size': 24,
            'color': 'black',
            'family': 'Arial, sans-serif'
        }
    },
    margin={"r": 20, "t": 80, "l": 20, "b": 20},  # add padding around map
    paper_bgcolor='white',  # white “board” behind everything
    plot_bgcolor='white',
    coloraxis_colorbar=dict(
        title="Dispatch Count",
        thickness=20,
        tickfont=dict(size=12)
    )
)
# -----------------------
# Show and export HTML
# -----------------------
fig.show()
fig.write_html(OUTPUT_HTML, include_plotlyjs='cdn', full_html=True)
