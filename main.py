import streamlit as st
import requests
import pandas as pd
import folium
from streamlit_folium import st_folium

# ==========================================
# FUNZIONI DI CALCOLO
# ==========================================

def calcola_potenza_eolica(v_vento_ms, p_nominale_w=1000.0):
    v_cut_in, v_rated, v_cut_out = 3.0, 12.0, 25.0
    if v_vento_ms < v_cut_in or v_vento_ms > v_cut_out: return 0.0
    elif v_cut_in <= v_vento_ms < v_rated:
        return p_nominale_w * ((v_vento_ms**3 - v_cut_in**3) / (v_rated**3 - v_cut_in**3))
    return p_nominale_w

@st.cache_data(show_spinner=False)
def scarica_profili_energia(lat, lon, tipo_tracker):
    url_pv = "https://re.jrc.ec.europa.eu/api/v5_2/seriescalc"
    params_pv = {"lat": lat, "lon": lon, "pvcalculation": 1, "peakpower": 1.0, "loss": 14.0, "outputformat": "json", "startyear": 2020, "endyear": 2020}
    if tipo_tracker == 0: params_pv["trackingtype"], params_pv["optimalangles"] = 0, 1
    elif tipo_tracker == 5: params_pv["trackingtype"], params_pv["optimalangles"] = 5, 1
    else: params_pv["trackingtype"] = tipo_tracker
        
    resp_pv = requests.get(url_pv, params=params_pv)
    if resp_pv.status_code != 200: return None
    df_pv = pd.DataFrame(resp_pv.json()['outputs']['hourly'])
    df_pv['Data_Ora'] = pd.to_datetime(df_pv['time'], format='%Y%m%d:%H%M').dt.floor('h')
    df_pv.rename(columns={'P': 'FV_1kW_W'}, inplace=True)
    
    url_wind = "https://archive-api.open-meteo.com/v1/archive"
    params_wind = {"latitude": lat, "longitude": lon, "start_date": "2020-01-01", "end_date": "2020-12-31", "hourly": "windspeed_100m", "wind_speed_unit": "ms", "timezone": "UTC"}
    resp_wind = requests.get(url_wind, params=params_wind)
    if resp_wind.status_code != 200: return None
    df_wind = pd.DataFrame({'Data_Ora': pd.to_datetime(resp_wind.json()['hourly']['time']).floor('h'), 'Vento_ms': resp_wind.json()['hourly']['windspeed_100m']})
    df_wind['Eolico_1kW_W'] = df_wind['Vento_ms'].apply(calcola_potenza_eolica)
    
    return pd.merge(df_pv[['Data_Ora', 'FV_1kW_W']], df_wind[['Data_Ora', 'Eolico_1kW_W']], on='Data_Ora', how='inner')

def esegui_simulazione_avanzata(df, mult_fv, mult_eolico, carico_w, batt_wh, p_gen_w, start_soc_p, stop_soc_p):
    soc = batt_wh * 0.5 # Partiamo al 50%
    gen_on = False
    ore_backup, ore_blackout = 0, 0
    e_tagliata, e_gen, e_fv, e_wind = 0.0, 0.0, 0.0, 0.0
    storia_soc = []
    
    soc_start = batt_wh * (start_soc_p / 100)
    soc_stop = batt_wh * (stop_soc_p / 100)

    for _, row in df.iterrows():
        p_fv, p_wind = row['FV_1kW_W'] * mult_fv, row['Eolico_1kW_W'] * mult_eolico
        p_rinnovabile = p_fv + p_wind
        e_fv += p_fv
        e_wind += p_wind
        
        # Logica Isteresi Generatore
        if soc <= soc_start: gen_on = True
        if soc >= soc_stop: gen_on = False
        
        p_gen_ora = p_gen_w if gen_on else 0.0
        if gen_on: 
            ore_backup += 1
            e_gen += p_gen_ora
        
        # Bilancio: Rinnovabili + Generatore vs Carico
        p_totale = p_rinnovabile + p_gen_ora
        
        if p_totale >= carico_w:
            surplus = p_totale - carico_w
            immessa = min(surplus, batt_wh - soc)
            soc += immessa
            e_tagliata += (surplus - immessa)
        else:
            deficit = carico_w - p_totale
            prelievo_batt = min(soc, deficit)
            soc -= prelievo_batt
            if (deficit - prelievo_batt) > 0.1: ore_blackout += 1
            
        storia_soc.append(soc)

    richiesta_kwh = (carico_w * len(df)) / 1000
    autarchia = ((richiesta_kwh * 1000 - (ore_blackout * carico_w) - e_gen) / (richiesta_kwh * 1000)) * 100
    
    return {
        "autarchia": max(0, autarchia), "ore_gen": ore_backup, "ore_blackout": ore_blackout,
        "gen_kwh": e_gen / 1000, "fv_kwh": e_fv / 1000, "wind_kwh": e_wind / 1000,
        "tagliata_kwh": e_tagliata / 1000, "storia_soc": storia_soc
    }

# ==========================================
# INTERFACCIA
# ==========================================

st.set_page_config(page_title="EMS Simulator", layout="wide")
st.title("🔋 Smart Hybrid EMS Simulator")

with st.expander("ℹ️ Logica di ricarica con generatore"):
    st.markdown("""
    Il generatore non si limita a coprire il buco energetico, ma agisce come un **caricabatterie intelligente**:
    - **Accensione:** Scatta quando la batteria scende sotto la soglia minima (Start SoC).
    - **Funzionamento:** Eroga la sua potenza massima per alimentare il carico e contemporaneamente ricaricare la batteria.
    - **Spegnimento:** Continua a girare finché la batteria non raggiunge la soglia di sicurezza (Stop SoC).
    """)

if "lat" not in st.session_state: st.session_state.lat, st.session_state.lon = 45.4642, 9.1900

col1, col2 = st.columns([1, 1.3])

with col1:
    st.subheader("1. Località")
    m = folium.Map(location=[st.session_state.lat, st.session_state.lon], zoom_start=5)
    folium.Marker([st.session_state.lat, st.session_state.lon]).add_to(m)
    map_data = st_folium(m, height=300, use_container_width=True)
    if map_data and map_data.get("last_clicked"):
        st.session_state.lat, st.session_state.lon = map_data["last_clicked"]["lat"], map_data["last_clicked"]["lng"]
        st.rerun()

with col2:
    st.subheader("2. Setup Hardware")
    tipo_pv = st.selectbox("Tecnologia FV:", ["Fisso (Ottimizzato)", "Insegue Inclinazione (E-W)", "Insegue Sole (N-S Inclinato)", "Asse Doppio"], index=0)
    map_pv = {"Fisso (Ottimizzato)": 0, "Insegue Inclinazione (E-W)": 4, "Insegue Sole (N-S Inclinato)": 5, "Asse Doppio": 2}
    
    c1, c2, c3 = st.columns(3)
    kw_fv = c1.number_input("FV (kWp)", 0.0, 50.0, 5.0)
    kw_wind = c2.number_input("Eolico (kW)", 0.0, 50.0, 2.0)
    kw_gen = c3.number_input("Generatore (kW)", 0.0, 50.0, 2.0)
    
    kwh_batt = c1.number_input("Batteria (kWh)", 1.0, 200.0, 15.0)
    mwh_anno = c2.number_input("MWh/anno richiesti", 0.1, 100.0, 8.76)
    
    st.subheader("3. Logica EMS (Soglie Batteria)")
    s1, s2 = st.columns(2)
    start_soc = s1.slider("Accendi Generatore al (%):", 5, 30, 15)
    stop_soc = s2.slider("Spegni Generatore al (%):", 40, 95, 70)
    
    esegui = st.button("🚀 Simula Anno Completo", use_container_width=True, type="primary")

if esegui:
    with st.spinner("Analisi satellitare in corso..."):
        df = scarica_profili_energia(st.session_state.lat, st.session_state.lon, map_pv[tipo_pv])
        if df is not None:
            w_carico = (mwh_anno * 1_000_000) / len(df)
            res = esegui_simulazione_avanzata(df, kw_fv, kw_wind, w_carico, kwh_batt*1000, kw_gen*1000, start_soc, stop_soc)
            
            st.divider()
            st.subheader("📊 Performance del Sistema")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Autarchia Rinnovabile", f"{res['autarchia']:.1f}%")
            m2.metric("Lavoro Generatore", f"{res['ore_gen']} ore")
            m3.metric("Energia Generatore", f"{res['gen_kwh']:.0f} kWh")
            m4.metric("Blackout", f"{res['ore_blackout']} ore")
            
            st.subheader("🔋 Ciclo di Carica/Scarica Batteria")
            df_plot = pd.DataFrame({"Data": df["Data_Ora"], "Batteria (kWh)": [v/1000 for v in res["storia_soc"]]}).set_index("Data")
            st.line_chart(df_plot)
            st.caption("Nota come il generatore 'riaggancia' la batteria verso l'alto quando tocca la soglia minima.")
