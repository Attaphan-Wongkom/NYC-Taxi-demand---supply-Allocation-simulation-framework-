import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
import json
import os
import math

# ==============================================================================
# Utility Functions
# ==============================================================================
def sanitize_text(text):
    """Sanitize text for Plotly hovertext to prevent JSON parse errors"""
    if text is None:
        return ''
    text = str(text)
    # Replace backslashes (main cause of Bad Unicode escape)
    text = text.replace('\\', '/')
    # Replace other problematic characters
    text = text.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
    return text

# ==============================================================================
# Page Config
# ==============================================================================
st.set_page_config(
    page_title="NYC Taxi Allocation Dashboard",
    page_icon="🗺️",
    layout="wide"
)

# ==============================================================================
# Data Loading
# ==============================================================================
@st.cache_data
def load_geojson():
    """Load NYC Taxi Zones GeoJSON from workspace folder"""
    geojson_path = os.path.join(os.path.dirname(__file__), "NYC_GeoJson.geojson")
    with open(geojson_path, "r") as f:
        geojson = json.load(f)
    
    # Sanitize zone names in GeoJSON to prevent JSON parse errors
    for feature in geojson.get('features', []):
        props = feature.get('properties', {})
        if 'zone' in props:
            props['zone'] = sanitize_text(props['zone'])
        if 'borough' in props:
            props['borough'] = sanitize_text(props['borough'])
    
    return geojson

@st.cache_data
def get_zone_coordinates():
    """Extract zone centroids from GeoJSON"""
    geojson = load_geojson()
    coords = {}
    for feature in geojson['features']:
        props = feature['properties']
        loc_id = int(props['locationid'])
        geom = feature['geometry']
        if geom['type'] == 'MultiPolygon':
            coords_list = []
            for polygon in geom['coordinates']:
                for ring in polygon:
                    coords_list.extend(ring)
        else:
            coords_list = []
            for ring in geom['coordinates']:
                coords_list.extend(ring)
        if coords_list:
            lons = [c[0] for c in coords_list]
            lats = [c[1] for c in coords_list]
            coords[loc_id] = (float(np.mean(lats)), float(np.mean(lons)))
    return coords

@st.cache_data
def load_real_data():
    """Load enhanced NYC taxi data with tip, ratecode, trip_duration"""
    parquet_path = os.path.join(os.path.dirname(__file__), "zone_hourly_enhanced.parquet")
    df = pd.read_parquet(parquet_path)
    df['locationid'] = df['locationid'].astype(int)
    df['hour'] = df['hour'].astype(int)
    df['demand'] = df['demand'].astype(int)
    df['supply'] = df['supply'].astype(int)
    df['ratio'] = df['ratio'].astype(float)
    df['avg_tip'] = df['avg_tip'].astype(float)
    df['avg_trip_duration'] = df['avg_trip_duration'].astype(float)
    return df

# ==============================================================================
# Scenario & Allocation Functions
# ==============================================================================
def apply_scenario(df, demand_pct_change, supply_adjustment=0):
    """Apply demand % change and supply adjustment scenario"""
    df_s = df.copy()
    df_s['demand_adjusted'] = df_s['demand'] * (1 + demand_pct_change / 100.0)
    df_s['supply_adjusted'] = (df_s['supply'] + supply_adjustment).clip(lower=0)
    df_s['ratio_adjusted'] = df_s['demand_adjusted'] / df_s['supply_adjusted'].replace(0, 1)
    df_s['status'] = np.where(
        df_s['ratio_adjusted'] > 1.2, 'Shortage',
        np.where(df_s['ratio_adjusted'] < 0.8, 'Overflow', 'Balanced')
    )
    return df_s

def compute_allocation_plan(df_hour, zone_coords, target_hour=18):
    """Compute taxi allocation plan — move from overflow to shortage zones
    target_hour: the hour we're optimizing for (for departure timing)"""
    overflow = df_hour[df_hour['status'] == 'Overflow'].copy()
    shortage = df_hour[df_hour['status'] == 'Shortage'].copy()
    
    if len(overflow) == 0 or len(shortage) == 0:
        return [], df_hour.copy()
    
    overflow['excess'] = (overflow['supply_adjusted'] * 0.8 - overflow['demand_adjusted']).clip(lower=0)
    shortage['shortfall'] = (shortage['demand_adjusted'] - shortage['supply_adjusted'] * 1.2).clip(lower=0)
    
    overflow['lat'] = overflow['locationid'].map(lambda x: zone_coords.get(x, (0, 0))[0])
    overflow['lon'] = overflow['locationid'].map(lambda x: zone_coords.get(x, (0, 0))[1])
    shortage['lat'] = shortage['locationid'].map(lambda x: zone_coords.get(x, (0, 0))[0])
    shortage['lon'] = shortage['locationid'].map(lambda x: zone_coords.get(x, (0, 0))[1])
    
    # Borough adjacency: which boroughs can allocate to each other (via bridges/land)
    # Staten Island and EWR are isolated (prevent water crossings)
    borough_adjacency = {
        "Manhattan": {"Manhattan", "Bronx", "Brooklyn", "Queens"},
        "Brooklyn": {"Brooklyn", "Manhattan", "Queens"},
        "Queens": {"Queens", "Manhattan", "Brooklyn"},
        "Bronx": {"Bronx", "Manhattan"},
        "Staten Island": {"Staten Island"},
        "EWR": {"EWR"},
    }
    max_distance = 0.15  # ~16km, prevents long-distance allocation across the city
    
    # Distance matrix with numpy
    o_coords = overflow[['lat', 'lon']].values
    s_coords = shortage[['lat', 'lon']].values
    dist = np.sqrt(((o_coords[:, None, :] - s_coords[None, :, :]) ** 2).sum(axis=2))
    
    allocations = []
    overflow_rem = overflow['excess'].values.astype(float).copy()
    shortage_rem = shortage['shortfall'].values.astype(float).copy()
    overflow_borough = overflow['borough'].values
    shortage_borough = shortage['borough'].values
    
    for i in range(len(overflow)):
        o_borough = overflow_borough[i]
        allowed_boroughs = borough_adjacency.get(o_borough, {o_borough})
        for j in np.argsort(dist[i]):
            if overflow_rem[i] <= 0 or shortage_rem[j] <= 0:
                continue
            # Skip if boroughs are not adjacent (prevents water crossings)
            if shortage_borough[j] not in allowed_boroughs:
                continue
            # Skip if too far (distances are sorted, so we can break)
            if dist[i][j] > max_distance:
                break
            # Estimate travel time from distance (~40 km/h avg in NYC)
            # dist is in degrees; 1 degree ≈ 111 km → convert to km, divide by speed, convert to minutes
            dist_km = float(dist[i][j]) * 111
            est_travel_min = round((dist_km / 40) * 60, 1)
            # Skip if travel time > 30 minutes (cost constraint)
            if est_travel_min > 30:
                continue
            amount = min(overflow_rem[i], shortage_rem[j])
            if amount > 10:
                # Get tip info for destination zone (incentive-based)
                dest_tip = float(shortage.iloc[j]['avg_tip']) if 'avg_tip' in shortage.columns else 0
                dest_is_airport = bool(shortage.iloc[j]['is_airport']) if 'is_airport' in shortage.columns else False
                # Calculate recommended departure time (must arrive before target hour + 5 min buffer)
                total_min_before = int(est_travel_min) + 5
                depart_total_min = target_hour * 60 - total_min_before
                while depart_total_min < 0:
                    depart_total_min += 24 * 60
                depart_h = int(depart_total_min // 60) % 24
                depart_m = int(depart_total_min % 60)
                recommended_departure = f"{depart_h:02d}:{depart_m:02d}"
                # --- Cost-Benefit Analysis ---
                # Revenue recovered: taxis serve trips at destination after arrival
                avail_min = max(0, 60 - est_travel_min)
                trips_per_taxi = max(1, avail_min / 15)  # ~15 min per trip cycle
                gross_fare = 52.0 if dest_is_airport else 13.0  # JFK flat $52, standard $13
                driver_earning_ratio = 0.65  # Driver keeps ~65% after expenses
                avg_fare = gross_fare * driver_earning_ratio
                revenue_per_trip = avg_fare + dest_tip
                revenue_at_shortage = trips_per_taxi * revenue_per_trip
                trips_at_overflow = trips_per_taxi * 0.3  # Overflow zones have ~30% of shortage demand
                revenue_at_overflow = trips_at_overflow * revenue_per_trip
                revenue_recovered = round(amount * (revenue_at_shortage - revenue_at_overflow), 2)
                # Allocation cost: lost earning while traveling + fuel
                lost_earning = (est_travel_min / 60.0) * 30.0  # $30/hour avg taxi earning
                fuel_cost = dist_km * 0.15  # $0.15/km
                allocation_cost = round(amount * (lost_earning + fuel_cost), 2)
                net_benefit = round(revenue_recovered - allocation_cost, 2)
                net_benefit_per_trip = round(net_benefit / amount, 2) if amount > 0 else 0
                # Cost-effectiveness label (per-trip basis — route quality independent of fleet size)
                if net_benefit_per_trip > 20:
                    cost_label = '✅ คุ้มค่า'
                elif net_benefit_per_trip > 0:
                    cost_label = '🟡 พอใช้'
                else:
                    cost_label = '❌ ไม่คุ้ม'
                allocations.append({
                    'from_zone': str(overflow.iloc[i]['zone']),
                    'from_id': int(overflow.iloc[i]['locationid']),
                    'from_lat': float(overflow.iloc[i]['lat']),
                    'from_lon': float(overflow.iloc[i]['lon']),
                    'to_zone': str(shortage.iloc[j]['zone']),
                    'to_id': int(shortage.iloc[j]['locationid']),
                    'to_lat': float(shortage.iloc[j]['lat']),
                    'to_lon': float(shortage.iloc[j]['lon']),
                    'amount': int(amount),
                    'est_travel_time': est_travel_min,
                    'dest_tip': round(dest_tip, 2),
                    'dest_is_airport': dest_is_airport,
                    'recommended_departure': recommended_departure,
                    'send_before_min': total_min_before,
                    'revenue_recovered': revenue_recovered,
                    'allocation_cost': allocation_cost,
                    'net_benefit': net_benefit,
                    'net_benefit_per_trip': net_benefit_per_trip,
                    'cost_label': cost_label
                })
                overflow_rem[i] -= amount
                shortage_rem[j] -= amount
    
    df_after = df_hour.copy()
    for alloc in allocations:
        df_after.loc[df_after['locationid'] == alloc['from_id'], 'supply_adjusted'] =             df_after.loc[df_after['locationid'] == alloc['from_id'], 'supply_adjusted'].values[0] - alloc['amount']
        df_after.loc[df_after['locationid'] == alloc['to_id'], 'supply_adjusted'] =             df_after.loc[df_after['locationid'] == alloc['to_id'], 'supply_adjusted'].values[0] + alloc['amount']
    
    df_after['ratio_adjusted'] = df_after['demand_adjusted'] / df_after['supply_adjusted'].replace(0, 1)
    df_after['status'] = np.where(
        df_after['ratio_adjusted'] > 1.2, 'Shortage',
        np.where(df_after['ratio_adjusted'] < 0.8, 'Overflow', 'Balanced')
    )
    return allocations, df_after

# ==============================================================================
# Wave Deployment System
# ==============================================================================
# Distributes taxi allocation across 6 waves per hour to reduce staging congestion
# Rationale: rush-hour commuters arrive first → front-load early waves
WAVE_WEIGHTS = [0.25, 0.25, 0.20, 0.15, 0.10, 0.05]  # 6 waves × 10 min

def compute_wave_schedule(allocations, target_hour):
    """Split each allocation into 6 deployment waves across the hour.
    
    Hour divided into 6 × 10-minute windows:
      Wave 1 (0-10 min):  25% — rush commuters, need taxis first
      Wave 2 (10-20 min): 25% — still high demand
      Wave 3 (20-30 min): 20% — moderate demand
      Wave 4 (30-40 min): 15% — demand tapering
      Wave 5 (40-50 min): 10% — relaxed riders
      Wave 6 (50-60 min):  5% — last-minute riders
    
    Each wave departs at: arrival_window_start - travel_time - 5 min buffer
    Reduces staging area congestion + traffic by spreading deployment across the hour.
    """
    wave_rows = []
    for alloc in allocations:
        amount = alloc['amount']
        travel_min = int(alloc['est_travel_time'])
        for w_idx, weight in enumerate(WAVE_WEIGHTS):
            wave_amount = int(round(amount * weight))
            # Ensure early waves get at least 1 taxi if total > 0
            if wave_amount == 0 and w_idx < 2 and amount > 0:
                wave_amount = 1
            if wave_amount == 0:
                continue
            arrival_start = w_idx * 10
            arrival_end = min(60, arrival_start + 10)
            depart_total_min = target_hour * 60 + arrival_start - travel_min - 5
            while depart_total_min < 0:
                depart_total_min += 24 * 60
            depart_h = int(depart_total_min // 60) % 24
            depart_m = int(depart_total_min % 60)
            # Sanitize zone names in route to prevent JSON parse errors
            from_zone_safe = sanitize_text(alloc['from_zone'])
            to_zone_safe = sanitize_text(alloc['to_zone'])
            
            wave_rows.append({
                'route': f"{from_zone_safe} → {to_zone_safe}",
                'wave': w_idx + 1,
                'arrival_window': f"{target_hour:02d}:{arrival_start:02d}-{arrival_end:02d}",
                'taxis': wave_amount,
                'weight': f"{weight*100:.0f}%",
                'departure': f"{depart_h:02d}:{depart_m:02d}",
            })
    return pd.DataFrame(wave_rows)

def create_allocation_arrows(allocations):
    """Create Plotly traces for allocation arrows with arrowheads"""
    if not allocations:
        return []
    traces = []
    for alloc in allocations:
        width = max(2, min(10, alloc['amount'] / 50))
        
        # Arrow line (sanitize zone names to prevent JSON parse errors)
        from_zone_safe = sanitize_text(alloc['from_zone'])
        to_zone_safe = sanitize_text(alloc['to_zone'])
        cost_label_safe = sanitize_text(alloc.get('cost_label', '?'))
        
        traces.append(go.Scattermapbox(
            lat=[alloc['from_lat'], alloc['to_lat']],
            lon=[alloc['from_lon'], alloc['to_lon']],
            mode='lines',
            line=dict(width=width, color='rgba(255, 140, 0, 0.8)'),
            hoverinfo='text',
            hovertext=f"🚕 {from_zone_safe} ➡️ {to_zone_safe}: {alloc['amount']:,} trips | ⏱️ ~{alloc.get('est_travel_time', '?')} นาที | 🕐 ส่งก่อน {alloc.get('recommended_departure', '?')} น. | {cost_label_safe} (Net {alloc.get('net_benefit_per_trip', 0):,.1f} USD/trip) | 💰 ทิป ~{alloc.get('dest_tip', '?')} USD",
            showlegend=False
        ))
        
        # Arrowhead at destination (triangle marker)
        marker_size = max(8, min(20, alloc['amount'] / 30))
        traces.append(go.Scattermapbox(
            lat=[alloc['to_lat']],
            lon=[alloc['to_lon']],
            mode='markers',
            marker=dict(
                size=marker_size,
                color='rgba(255, 100, 0, 1)',
                symbol='triangle',
                angle=0  # Plotly mapbox doesn't support rotation, but triangle still looks like arrowhead
            ),
            hoverinfo='skip',
            showlegend=False
        ))
    
    return traces

def make_choropleth(df, geojson, color_col, color_scale, color_range, title=""):
    """Create a choropleth map — centralized to avoid code duplication"""
    hover_dict = {
        'locationid': False,
        'borough': True,
        'demand_adjusted': ':,.0f',
        'supply_adjusted': ':,.0f',
        'ratio_adjusted': ':.2f',
        'status': True,
        'avg_tip': ':.2f',
        'is_airport': True,
        color_col: False
    }
    # Only include columns that exist
    hover_dict = {k: v for k, v in hover_dict.items() if k in df.columns or k == color_col}
    
    fig = px.choropleth_mapbox(
        df,
        geojson=geojson,
        locations='locationid',
        featureidkey="properties.locationid",
        color=color_col,
        color_continuous_scale=color_scale,
        range_color=color_range,
        mapbox_style="carto-positron",
        zoom=9.5,
        center={"lat": 40.7128, "lon": -74.0060},
        opacity=0.7,
        hover_name='zone',
        hover_data=hover_dict
    )
    fig.update_layout(height=500, margin={"r":0,"t":0,"l":0,"b":0})
    return fig

# ==============================================================================
# Main App
# ==============================================================================
st.title("🗺️ NYC Taxi Allocation & Simulation Dashboard 🚕")
st.markdown("""🎨 **แดชบอร์ดวิเคราะห์และจำลองสถานการณ์แท็กซี่ NYC** — เลือกโหมดการแสดงผล ปรับ slider สถานการณ์ และดูแผนจัดสรรแท็กซี่แบบเรียลไทม์ ✨""")

# Load data
st.cache_data.clear()  # Force fresh data load on every app restart
with st.spinner("🔄 กำลังโหลดข้อมูลและแผนที่..."):
    try:
        geojson = load_geojson()
        zone_data = load_real_data()
        zone_coords = get_zone_coordinates()
        # Verify avg_tip column exists
        if 'avg_tip' not in zone_data.columns:
            st.warning("⚠️ avg_tip column not found in data! Tip map will not work.")
    except FileNotFoundError as e:
        st.error(f"❌ ไม่พบไฟล์: {str(e)}")
        st.stop()

# ==============================================================================
# Sidebar Controls
# ==============================================================================
st.sidebar.header("⚙️ ตั้งค่าการแสดงผล")

view_mode = st.sidebar.radio(
    "🎯 เลือกโหมด",
    ["📊 Analysis Mode (วิเคราะห์ข้อมูล)", "🚕 Allocation Plan Mode (แผนจัดสรร)"],
    help="📊 Analysis = ดูความต้องการ/จำนวนแท็กซี่/สัดส่วน | 🚕 Allocation = แสดงแผนย้ายแท็กซี่จากเขตเกินไปเขตขาด"
)
allocation_mode = "Allocation" in view_mode

metric = st.sidebar.selectbox("📊 เลือก Metric", ["Ratio (Demand/Supply)", "Demand", "Supply", "💰 Avg Tip ($)"])
metric_col = {
    "Ratio (Demand/Supply)": "ratio", "Demand": "demand", "Supply": "supply",
    "💰 Avg Tip ($)": "avg_tip"
}[metric]

hour_selected = st.sidebar.slider("⏰ ชั่วโมง (Hour)", 0, 23, 18)
# Peak hour indicator
peak_hours = {7: "🌅 Morning Rush", 8: "🌅 Morning Rush", 9: "🌅 Morning Rush",
              17: "🌇 Evening Rush", 18: "🌇 Evening Rush", 19: "🌇 Evening Rush"}
if hour_selected in peak_hours:
    st.sidebar.markdown(f"**{peak_hours[hour_selected]}** 📍 ชั่วโมงเร่งด่วน — ความต้องการสูงสุด!")
elif hour_selected in [0, 1, 2, 3, 4]:
    st.sidebar.markdown("🌙 **Late Night** — ความต้องการต่ำ แท็กซี่ลดลง")
elif hour_selected in [10, 11, 12, 13, 14, 15, 16]:
    st.sidebar.markdown("☀️ **Daytime** — ความต้องการปานกลาง")
else:
    st.sidebar.markdown("🌆 **Evening** — ความต้องการเริ่มลด")

demand_change = st.sidebar.slider("📈 Demand Change (%)", -50, 50, 20, step=5, help="ปรับ % ความต้องการ (ลดหรือเพิ่ม)")
supply_adjustment = st.sidebar.slider("🚕 Supply Adjustment (taxis/hour)", -5000, 5000, 0, step=100, help="ปรับจำนวนแท็กซี่รายชั่วโมง (ลดหรือเพิ่ม)")

borough_options = ['All'] + sorted(zone_data['borough'].unique().tolist())
borough_filter = st.sidebar.multiselect("🏛️ Borough (เขต)", borough_options, default=['All'])

if not allocation_mode:
    zone_list = sorted(zone_data[['locationid', 'zone', 'borough']].drop_duplicates().apply(
        lambda x: f"{x['zone']} ({x['borough']})", axis=1
    ).tolist())
    zone_selected = st.sidebar.selectbox("📍 เลือก Zone (เขตย่อย)", ['(ไม่เลือก)'] + zone_list)
else:
    zone_selected = '(ไม่เลือก)'
    comparison_mode = st.sidebar.checkbox("🔄 เปรียบเทียบก่อน/หลัง Allocation", value=True)

if not allocation_mode:
    comparison_mode = False

st.sidebar.markdown("---")
st.sidebar.markdown("---")
# Dynamic legend based on metric
if metric_col == 'avg_tip':
    st.sidebar.markdown("""
### 💰 Legend: Avg Tip (USD)
🟢 **เข้ม** = ทิปสูง (10+ USD)
**กลาง** = ทิปปานกลาง (4-10 USD)
⚪ **อ่อน** = ทิปต่ำ (0-4 USD)

💡 **Tip Map** ช่วยให้คนขับเห็นว่า
ไปเขตไหนแล้วได้ทิปสูง = แรงจูงใจ (market-based)
""")
elif metric_col == 'ratio':
    st.sidebar.markdown("""
### 🎨 Legend: Ratio
🔴 **Shortage** = แท็กซี่ไม่พอ (Ratio > 1.2)
⚪ **Balanced** = สมดุล (0.8 ≤ Ratio ≤ 1.2)
🔵 **Overflow** = แท็กซี่เกิน (Ratio < 0.8)
""")
elif metric_col == 'demand':
    st.sidebar.markdown("""
### 📈 Legend: Demand
🔴 **เข้ม** = ความต้องการสูง (คนรอแท็กซี่เยอะ)
⚪ **อ่อน** = ความต้องการต่ำ
""")
else:  # supply
    st.sidebar.markdown("""
### 🚕 Legend: Supply
🔵 **เข้ม** = แท็กซี่เยอะ
⚪ **อ่อน** = แท็กซี่น้อย
""")

if allocation_mode:
    st.sidebar.markdown("""
### 🚕 Allocation Mode
🟠 **ลูกศรสีส้ม** = การย้ายแท็กซี่
   จาก Overflow → Shortage zones
""")

# ==============================================================================
# Filter Data
# ==============================================================================
zone_data_s = apply_scenario(zone_data, demand_change, supply_adjustment)
df_hour = zone_data_s[zone_data_s['hour'] == hour_selected].copy()

if 'All' not in borough_filter and len(borough_filter) > 0:
    df_hour = df_hour[df_hour['borough'].isin(borough_filter)].copy()

# Compute allocation
if allocation_mode:
    allocations, df_after = compute_allocation_plan(df_hour, zone_coords, hour_selected)
    total_moved = sum(a['amount'] for a in allocations)
else:
    allocations = []
    df_after = df_hour.copy()
    total_moved = 0

# Set metric column
if metric_col == 'ratio':
    df_hour['metric_value'] = df_hour['ratio_adjusted'].astype(float)
    df_after['metric_value'] = df_after['ratio_adjusted'].astype(float)
    color_scale = "RdBu_r"  # Blue(0) → White(0.5) → Red(1): white at ratio=1.0 = balanced
    color_range = [0.4, 1.6]  # Centered at 1.0 so white = balanced
elif metric_col == 'demand':
    df_hour['metric_value'] = df_hour['demand_adjusted'].astype(float)
    df_after['metric_value'] = df_after['demand_adjusted'].astype(float)
    color_scale = "Reds"
    color_range = [0, float(df_hour['metric_value'].quantile(0.95))]
elif metric_col == 'supply':
    df_hour['metric_value'] = df_hour['supply_adjusted'].astype(float)
    df_after['metric_value'] = df_after['supply_adjusted'].astype(float)
    color_scale = "Blues"
    color_range = [0, float(df_hour['metric_value'].quantile(0.95))]
elif metric_col == 'avg_tip':
    df_hour['metric_value'] = df_hour['avg_tip'].astype(float)
    df_after['metric_value'] = df_after['avg_tip'].astype(float) if 'avg_tip' in df_after.columns else 0
    color_scale = "Greens"
    color_range = [0, 15.0]  # Tips range $0-$15

# ==============================================================================
# Visualization
# ==============================================================================
if allocation_mode and comparison_mode:
    st.subheader(f"🔄 เปรียบเทียบก่อน vs หลัง Allocation @ {hour_selected:02d}:00 น. (Demand {demand_change:+d}%, Supply {supply_adjustment:+,d})")
    
    col_b, col_a = st.columns(2)
    
    with col_b:
        st.markdown("### 📊 ก่อน Allocation (Before)")
        fig_b = make_choropleth(df_hour, geojson, 'metric_value', color_scale, color_range)
        st.plotly_chart(fig_b, use_container_width=True)
        sb = (df_hour['status'] == 'Shortage').sum()
        ob = (df_hour['status'] == 'Overflow').sum()
        st.metric("🔴 Shortage Zones", int(sb))
        st.metric("🔵 Overflow Zones", int(ob))
    
    with col_a:
        st.markdown("### ✅ หลัง Allocation (After)")
        fig_a = make_choropleth(df_after, geojson, 'metric_value', color_scale, color_range)
        for trace in create_allocation_arrows(allocations):
            fig_a.add_trace(trace)
        st.plotly_chart(fig_a, use_container_width=True)
        sa = (df_after['status'] == 'Shortage').sum()
        oa = (df_after['status'] == 'Overflow').sum()
        st.metric("🔴 Shortage Zones", int(sa), delta=int(sa - sb))
        st.metric("🔵 Overflow Zones", int(oa), delta=int(oa - ob))
    
    st.markdown("---")
    total_rev = sum(a.get('revenue_recovered', 0) for a in allocations)  # Incremental revenue (before cost)
    total_cost = sum(a.get('allocation_cost', 0) for a in allocations)  # Allocation cost
    total_net = total_rev - total_cost  # Net Benefit (this is what we recover!)
    
    st.subheader(f"📋 สรุปแผนจัดสรร: {len(allocations)} เส้นทาง, {total_moved:,} trips")
    
    # Cost-benefit summary cards (4 columns)
    cb1, cb2, cb3, cb4 = st.columns(4)
    cb1.metric("💵 รายได้เพิ่มเติม (USD/ชม.)", f"{total_rev:,.0f} USD", help="รายได้เพิ่มเติมจากการย้ายแท็กซี่ไป shortage zones (ก่อนหักค่าย้าย) — เกิดจากความต้องการที่สูงขึ้น")
    cb2.metric("💸 ค่าใช้จ่ายการย้าย (USD/ชม.)", f"{total_cost:,.0f} USD", help="ค่าใช้จ่ายในการย้ายแท็กซี่ = เวลาเสียระหว่างขับว่าง + น้ำมัน")
    cb3.metric("🟢 ผลกำไรสุทธิ (USD/ชม.)", f"{total_net:,.0f} USD", help="ผลกำไรสุทธิจากระบบจัดสรร (= รายได้เพิ่มเติม - ค่าใช้จ่าย) — ตัวเลขนี้ที่กู้คืนได้จริง! 🎯")
    avg_net_per_trip = total_net / total_moved if total_moved > 0 else 0
    cb4.metric("📊 Net/trip (USD/trip/ชม.)", f"{avg_net_per_trip:,.1f} USD", delta=f"{'✅ คุ้มค่า' if avg_net_per_trip > 20 else '🟡 พอใช้' if avg_net_per_trip > 0 else '❌ ไม่คุ้ม'}", help="ผลกำไรสุทธิต่อ 1 trip — ใช้ตัดสินว่าเส้นทางไหนคุ้มค่า")
    
    # Summary: จำนวนเส้นทางแต่ละระดับ
    count_good = sum(1 for a in allocations if a.get('net_benefit_per_trip', 0) > 20)
    count_ok = sum(1 for a in allocations if 0 < a.get('net_benefit_per_trip', 0) <= 20)
    count_bad = sum(1 for a in allocations if a.get('net_benefit_per_trip', 0) <= 0)
    
    summary_cols = st.columns(3)
    summary_cols[0].metric("✅ คุ้มค่า", f"{count_good} เส้นทาง", help="Net/trip > 20 USD/trip")
    summary_cols[1].metric("🟡 พอใช้", f"{count_ok} เส้นทาง", help="Net/trip 0-20 USD/trip")
    summary_cols[2].metric("❌ ไม่คุ้ม", f"{count_bad} เส้นทาง", help="Net/trip < 0 USD/trip")
    st.caption("⚠️ **สมมติฐานการคำนวณ:** (1) Driver net earning = 65% ของ gross fare (หักค่าน้ำมัน, เสื่อมราคา, ค่าธรรมเนียม) + ทิป 100% | (2) Opportunity cost: overflow zones มี demand ~30% ของ shortage zones | (3) รายได้คำนวณจากการรับผู้โดยสารเต็มที่ในชั่วโมงที่เลือก — ค่าจริงจะต่ำกว่าเนื่องจาก supply เป็น proxy (dropoff count), คนขับพักงาน, และความคลาดเคลื่อนของโมเดลพยากรณ์ (MAPE ~33%)")
    st.caption("ℹ️ **หน่วยนับเป็น Trips ไม่ใช่คันรถ:** จำนวนที่แสดง (เช่น 25,675) คือจำนวน **trips** (dropoff count) ไม่ใช่จำนวนแท็กซี่จริง — แท็กซี่ 1 คันทำหลาย trip ต่อชั่วโมง ตามข้อมูล TLC แท็กซี่เหลือง NYC (~13,500 medallions) ทำเฉลี่ย ~2 trips/คัน/ชม. ในชั่วโมงเร่งด่วน → 25,675 trips ≈ ~12,800 คัน (ใกล้เคียง fleet จริง) | ดู README.md สำหรับการประมาณมูลค่าแบบ realistic",)
    if allocations:
        alloc_df = pd.DataFrame(allocations)
        display_cols = ['from_zone', 'to_zone', 'amount', 'est_travel_time', 'recommended_departure', 'revenue_recovered', 'allocation_cost', 'net_benefit', 'net_benefit_per_trip', 'cost_label', 'dest_tip', 'dest_is_airport']
        display_cols = [c for c in display_cols if c in alloc_df.columns]
        rename_map = {
            'from_zone': '🚕 จาก Zone', 'to_zone': '➡️ ไป Zone', 'amount': '🔢 จำนวน Trips',
            'est_travel_time': '⏱️ เวลาเดินทาง (นาที)', 'recommended_departure': '🕐 ส่งก่อนเวลา',
            'revenue_recovered': '💵 รายได้กู้คืน (USD/เส้นทาง/ชม.)', 'allocation_cost': '💸 ค่าย้าย (USD/เส้นทาง)',
            'net_benefit': '📊 Net Benefit (USD/เส้นทาง)', 'net_benefit_per_trip': '📊 Net/trip (USD/trip/ชม.)',
            'cost_label': '🎯 ความคุ้มค่า',
            'dest_tip': '💰 ทิปเฉลี่ย (USD/trip)', 'dest_is_airport': '✈️ Airport?'
        }
        st.dataframe(alloc_df[display_cols].rename(columns=rename_map), use_container_width=True, height=300)

    # --- Wave Deployment Schedule ---
    if allocations:
        wave_df = compute_wave_schedule(allocations, hour_selected)
        if len(wave_df) > 0:
            st.markdown("---")
            st.subheader("🌊 Wave Deployment Schedule")
            st.markdown(f"แบ่งการย้ายแท็กซี่ออกเป็น 6 ช่วง (10 นาทีต่อช่วง) ในชั่วโมง {hour_selected:02d}:00 น. เพื่อลดความแออัดที่จุดจอดรอ")
            
            wave_summary = wave_df.groupby(['wave', 'arrival_window', 'weight']).agg(
                total_taxis=('taxis', 'sum')
            ).reset_index()
            
            ws_cols = st.columns(6)
            wave_labels = ['🟢 W1 (25%)', '🟢 W2 (25%)', '🟡 W3 (20%)', '🟡 W4 (15%)', '🟠 W5 (10%)', '🔴 W6 (5%)']
            for idx, col in enumerate(ws_cols):
                if idx < len(wave_summary):
                    row = wave_summary.iloc[idx]
                    col.metric(wave_labels[idx], f"{int(row['total_taxis']):,} trips", help=f"ถึงช่วง {row['arrival_window']} น.")
                else:
                    col.metric(wave_labels[idx], "0 trips")
            
            with st.expander("📋 รายละเอียด Wave Schedule (แต่ละเส้นทาง)"):
                st.dataframe(wave_df.rename(columns={
                    'route': '🚕 เส้นทาง', 'wave': '🌊 Wave', 'arrival_window': '⏰ ช่วงถึง',
                    'taxis': '🔢 จำนวน Trips', 'weight': '📊 น้ำหนัก', 'departure': '🕐 ออกเดินทาง'
                }), use_container_width=True, height=300)
            
            st.caption("💡 Wave 1-2 (50%): คนรีบกลับบ้าน → ส่งรถเยอะ | Wave 5-6 (15%): คนนั่งชิว → ส่งรถน้อย | พอเริ่มชั่วโมงใหม่ก็เริ่ม Wave 1 ใหม่")

elif allocation_mode:
    st.subheader(f"🚕 แผนจัดสรร @ {hour_selected:02d}:00 น. (Demand {demand_change:+d}%, Supply {supply_adjustment:+,d})")
    fig = make_choropleth(df_hour, geojson, 'metric_value', color_scale, color_scale, color_range)
    for trace in create_allocation_arrows(allocations):
        fig.add_trace(trace)
    fig.update_layout(height=600)
    st.plotly_chart(fig, use_container_width=True)
    st.info(f"🟠 วางแผน {len(allocations)} เส้นทาง, ย้าย {total_moved:,} trips | ⏱️ กรองเฉพาะเส้นทาง ≤ 30 นาที")

else:
    st.subheader(f"🗺️ {metric} @ {hour_selected:02d}:00 น. (Demand {demand_change:+d}%, Supply {supply_adjustment:+,d})")
    fig = make_choropleth(df_hour, geojson, 'metric_value', color_scale, color_range)
    fig.update_layout(height=600)
    st.plotly_chart(fig, use_container_width=True)

# ==============================================================================
# Metrics Summary
# ==============================================================================
c1, c2, c3, c4 = st.columns(4)
total_z = len(df_hour)
c1.metric("🔴 Shortage Zones", int((df_hour['status'] == 'Shortage').sum()))
c2.metric("🔵 Overflow Zones", int((df_hour['status'] == 'Overflow').sum()))
c3.metric("🟢 Balanced Zones", int((df_hour['status'] == 'Balanced').sum()))
c4.metric("📊 Avg Ratio", f"{float(df_hour['ratio_adjusted'].mean()):.2f}")

# ==============================================================================
# Zone Detail Chart
# ==============================================================================
if not allocation_mode and zone_selected != '(None)':
    st.markdown("---")
    zone_name = zone_selected.split(' (')[0]
    zone_detail = zone_data_s[zone_data_s['zone'] == zone_name].copy()
    
    if len(zone_detail) > 0:
        st.subheader(f"📍 Zone: {zone_name} ({zone_detail.iloc[0]['borough']})")
        fig_z = go.Figure()
        fig_z.add_trace(go.Scatter(
            x=zone_detail['hour'], y=zone_detail['demand_adjusted'],
            name='📈 Demand', mode='lines+markers', line=dict(color='#e74c3c', width=3)
        ))
        fig_z.add_trace(go.Scatter(
            x=zone_detail['hour'], y=zone_detail['supply_adjusted'],
            name='🚕 Supply', mode='lines+markers', line=dict(color='#3498db', width=3)
        ))
        fig_z.add_trace(go.Scatter(
            x=zone_detail['hour'], y=zone_detail['ratio_adjusted'],
            name='📊 Ratio', mode='lines+markers', line=dict(color='#f39c12', width=2, dash='dash'),
            yaxis='y2'
        ))
        fig_z.add_hline(y=1.2, line_dash="dot", line_color="red", opacity=0.5, yref='y2')
        fig_z.add_hline(y=0.8, line_dash="dot", line_color="blue", opacity=0.5, yref='y2')
        fig_z.add_vline(x=hour_selected, line_dash="dash", line_color="purple")
        fig_z.update_layout(
            xaxis_title="⏰ ชั่วโมง", yaxis_title="🚗 จำนวน Trips",
            yaxis2=dict(title="📊 Ratio", overlaying='y', side='right', range=[0, 3]),
            hovermode="x unified", height=400
        )
        st.plotly_chart(fig_z, use_container_width=True)

# ==============================================================================
# Borough Trend
# ==============================================================================
if not allocation_mode:
    st.markdown("---")
    st.subheader("📉 แนวโน้ม 24 ชั่วโมงตาม Borough")
    borough_trend = zone_data_s.groupby(['borough', 'hour']).agg({
        'demand_adjusted': 'sum',
        'supply_adjusted': 'sum',
        'ratio_adjusted': 'mean'
    }).reset_index()
    
    if 'All' not in borough_filter and len(borough_filter) > 0:
        borough_trend = borough_trend[borough_trend['borough'].isin(borough_filter)]
    
    trend_col = 'ratio_adjusted' if metric_col == 'ratio' else metric_col
    fig_t = px.line(borough_trend, x='hour', y=trend_col, color='borough', markers=True)
    fig_t.update_layout(height=300, hovermode="x unified")
    fig_t.add_vline(x=hour_selected, line_dash="dash", line_color="red")
    st.plotly_chart(fig_t, use_container_width=True)

st.markdown("---")

if allocation_mode:
    with st.expander("📖 คู่มือการใช้งาน Allocation Plan", expanded=False):
        st.markdown("""
### 🎯 วิธีอ่านแผนจัดสรร

#### 🟠 ลูกศรสีส้มบนแผนที่
ลูกศรแสดงการย้ายแท็กซี่จากเขตที่มีแท็กซี่เกิน (**Overflow** 🔵) ไปยังเขตที่ขาดแท็กซี่ (**Shortage** 🔴)
- **ความหนาของเส้น** = จำนวนแท็กซี่ที่แนะนำให้ย้าย (เส้นหนา = ย้ายเยอะ)
- **Hover บนลูกศร** = ดูรายละเอียด เช่น "Zone A ➡️ Zone B: 500 trips | ⏱️ ~15 นาที | 💰 ทิป ~$6.12"

#### 💰 Tip Map (Incentive-Based)
ในตารางสรุปแผนจัดสรร มีคอลัมน์ **💰 ทิปเฉลี่ย ($)** ที่บอกคนขับว่า:
- ไป Zone ไหนแล้วได้ทิปสูง → คนขับมีแรงจูงใจไปเอง (market-based)
- ไม่ต้องบังคับย้ายรถ แต่ใช้ทิปเป็นแรงจูงใจ
- **ตัวอย่าง:** "ไป Queens (JFK) ได้ทิป 12.21 USD + ขาดรถ = ไปเลย!"

#### ✈️ RatecodeID (ประเภท Trip)
คอลัมน์ **✈️ Airport?** บอกว่า Zone ปลายทางเป็นเขตสนามบินหรือไม่:
- **✅ Yes** = Airport zone → ส่งรถเป็น batch ตาม flight schedule (JFK flat fare 52 USD)
- **❌ No** = Local zone → ส่งรถตาม demand pattern ปกติ (Standard rate)

#### ⏱️ Trip Duration (Timing Constraint)
คอลัมน์ **⏱️ เวลาเดินทาง (นาที)** คำนวณจากระยะทางระหว่าง zone:
- **≤ 30 นาที** = ย้ายได้ (กรองเส้นทางที่ไกลเกินไปออก)
- ทำให้แผนจัดสรรสมจริง ไม่แนะนำให้ขับข้ามเมือง

#### 📊 Cost-Benefit Analysis (ความคุ้มค่า)
เปรียบเทียบ **รายได้ที่กู้คืนได้** vs **ค่าใช้จ่ายการย้าย** สำหรับผู้บริหารเมือง:

**💵 รายได้กู้คืน (USD/เส้นทาง/ชม.)** = แท็กซี่ที่ไปถึง x trips ที่รับได้ x รายได้/trip
- รายได้/trip = 13 USD (standard) หรือ 52 USD (JFK flat) + ทิป
- หน่วย: ดอลลาร์ต่อเส้นทางต่อชั่วโมง (รายได้ที่จะเกิดขึ้นใน 1 ชม. หลังแท็กซี่ไปถึงโซนปลายทาง)

**💸 ค่าย้าย (USD/เส้นทาง)** = จำนวน trips x (เวลาเสียระหว่างขับ + น้ำมัน)
- เวลาเสีย = 30 USD/ชม. (รายได้ที่เสียไประหว่างขับว่างไปโซนใหม่)
- น้ำมัน = 0.15 USD/km
- หน่วย: ดอลลาร์ต่อเส้นทาง (ค่าใช้จ่ายครั้งเดียวสำหรับย้ายแท็กซี่ทั้งหมดในเส้นทางนี้)

**📊 Net Benefit (USD/เส้นทาง)** = รายได้กู้คืน - ค่าย้าย
- หน่วย: ดอลลาร์ต่อเส้นทาง (กำไรสุทธิของการย้ายแท็กซี่ทั้งหมดในเส้นทางนี้ สำหรับชั่วโมงที่เลือก)

**📊 Net/trip (USD/trip/ชม.)** = Net Benefit ÷ จำนวน trips
- หน่วย: ดอลลาร์ต่อ trip ต่อชั่วโมง (กำไรสุทธิต่อ 1 trip ที่ย้ายในเส้นทางนี้)
- ทำไมต้องดูต่อ trip: เส้นทางที่ย้าย 150 trips อาจมี Net สูงแค่เพราะ trips เยอะ แต่ถ้าดูต่อ trip จะเห็นว่าเส้นทางไหนคุ้มจริง
- ⚠️ จำนวนที่แสดงเป็น **trips** (dropoff count) ไม่ใช่จำนวนแท็กซี่จริง — แท็กซี่ 1 คันทำ ~2 trips/ชม. ตามข้อมูล TLC

**🎯 ความคุ้มค่า** แบ่ง 3 ระดับ (Net/trip ต่อเส้นทาง):
- **✅ คุ้มค่า** = Net/trip > 20 USD/trip (รถหาเงินได้ 2-3 เท่าของค่าย้าย ควรทำ!)
- **🟡 พอใช้** = Net/trip 0-20 USD/trip (กำไรน้อย อาจไม่คุ้มค่า management overhead)
- **❌ ไม่คุ้ม** = Net/trip < 0 USD/trip (เสียเงิน ควรหาทางอื่น)

#### 🌊 Wave Deployment (การแบ่งช่วงส่งรถ)
แทนที่จะส่งแท็กซี่ทั้งหมดพร้อมกัน ระบบแบ่งเป็น **6 ช่วง × 10 นาที** ต่อชั่วโมง:

| Wave | ช่วงเวลา | น้ำหนัก | เหตุผล |
|---|---|---|---|
| 1 | นาที 0-10 | 25% | คนรีบกลับบ้าน → ต้องการรถเร็ว |
| 2 | นาที 10-20 | 25% | ยังคง demand สูง |
| 3 | นาที 20-30 | 20% | demand ปานกลาง |
| 4 | นาที 30-40 | 15% | demand ลดลง |
| 5 | นาที 40-50 | 10% | คนนั่งชิว ไม่รีบ |
| 6 | นาที 50-60 | 5% | คนที่เหลือ |

- แต่ละ Wave มี **เวลาออกเดินทาง** คำนวณจาก: ช่วงถึง - เวลาเดินทาง - 5 นาที buffer
- ส่งรถเยอะตอนต้นชั่วโมง → ลดความแออัดที่จุดจอด + จราจร
- พอเริ่มชั่วโมงใหม่ → เริ่ม Wave 1 ใหม่

💡 **สำหรับผู้บริหารเมือง:** ดู Net Benefit รวมด้านบนตารางเพื่อตัดสินใจว่าแผนจัดสรรทั้งหมดคุ้มไหม
💡 **สำหรับคนขับแท็กซี่:** ดู Tip Map + ความคุ้มค่าแต่ละเส้นทาง เพื่อตัดสินใจเอง

### 📋 วิธีใช้ Dashboard นี้

1. **เลือก Hour** ⏰ — ดูชั่วโมงที่สนใจ (เช่น 18:00 น. = ช่วงเย็น)
2. **ปรับ Demand/Supply** 🎚️ — จำลองสถานการณ์ (เช่น Demand +30%, Supply -1,000)
3. **เปิด Comparison Mode** 🔄 — เปรียบเทียบก่อน/หลัง Allocation
4. **ดูลูกศร** 🟠 — hover เพื่อดูความคุ้มค่าของแต่ละเส้นทาง
5. **ดูตารางสรุป** 📋 — ดู Net Benefit, ทิป, เวลาเดินทาง, และความคุ้มค่า
6. **ดู Summary Cards** 💵💸📊 — รายได้รวม vs ค่าใช้จ่ายรวม vs Net Benefit
7. **เลือก Metric** 💰 — สลับไปดู Avg Tip Map เพื่อดูว่า Zone ไหนทิปสูง
        """)

st.markdown("---")
st.caption("🗺️ NYC Taxi Zones: 263 zones | 📊 Real Trip Data (Jan 2018, 8.7M trips) | 💰 Tip (USD) + ✈️ RatecodeID + ⏱️ Trip Duration + 📊 Cost-Benefit | 🚀 Streamlit + Plotly")
