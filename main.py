import streamlit as st
import requests
import pandas as pd
import folium
from streamlit_folium import st_folium

# ==========================================
# FUNZIONI DI CALCOLO (Con Caching)
# ==========================================

def calcola_potenza_eolica(v_vento_ms, p_nominale_w=1000.0):
    """
    Applica una curva di potenza semplificata per una turbina da 1 kW.
    v_vento_ms: velocità del vento in metri al secondo.
    """
    v_cut_in = 3.0    # Vento minimo per partire
    v_rated = 12.0    # Vento per potenza massima
    v_cut_out = 25.0  # Vento massimo (spegnimento di sicurezza)
    
    if v_vento_ms < v_cut_in or v_vento_ms > v_cut_out:
        return 0.0
    elif v_cut_in <= v_vento_ms < v_rated:
        # Interpolazione cubica (la potenza del vento cresce col cubo della velocità)
        return p_nominale_w * ((v_vento_ms**3 - v_cut_in**3) / (v_rated**3 - v_cut_in**3))
    else:
        return p_nominale_w

@st.cache_data(show_spinner=False)
def scarica_profili_energia(lat, lon, tipo_tracker):
    """Scarica e allinea i dati FV da PVGIS e i dati del vento da Open-Meteo"""
    
    # 1. SCARICA FOTOVOLTAICO (PVGIS)
    url_pv = "https://re.jrc.ec.europa.eu/api/v5_2/seriescalc"
    params_pv = {
        "lat": lat, "lon": lon, "pvcalculation": 1,
        "peakpower": 1.0, "loss": 14.0, "trackingtype": tipo_tracker, 
        "outputformat": "json", "startyear": 2020, "endyear": 2020
    }
    resp_pv = requests.get(url_pv, params=params_pv)
    if resp_pv.status_code != 200:
        return None
    
    data_pv = resp_pv.json()
    df_pv = pd.DataFrame(data_pv['outputs']['hourly'])
    df_pv['Data_Ora'] = pd.to_datetime(df_pv['time'], format='%Y%m%d:%H%M')
    df_pv.rename(columns={'P': 'FV_1kW_W'}, inplace=True)
    df_pv = df_pv[['Data_Ora', 'FV_1kW_W']]
    
    # 2. SCARICA VENTO (Open-Meteo Historical API)
    url_wind = "https://archive-api.open-meteo.com/v1/archive"
    params_wind = {
        "latitude": lat, "longitude": lon,
        "start_date": "2020-01-01", "end_date": "2020-12-31",
        "hourly": "windspeed_100m", "wind_speed_unit": "ms", # Vento a 100m in metri al secondo
        "timezone": "UTC"
    }
    resp_wind = requests.get(url_wind, params=params_wind)
    if resp_wind.status_code != 200:
        return None
        
    data_wind = resp_wind.json()
    df_wind = pd.DataFrame({
        'Data_Ora': pd.to_datetime(data_wind['hourly']['time']),
        'Vento_ms': data_wind['hourly']['windspeed_100m']
    })
    
    # Applica la curva di potenza per ottenere il profilo di 1 kW eolico
    df_wind['Eolico_1kW_W'] = df_wind['Vento_ms'].apply(calcola_potenza_eolica)
    df_wind = df_wind[['Data_Ora', 'Eolico_1kW_W']]
    
    # 3. UNISCI I DATI (Merge sull'ora esatta per evitare sfasamenti)
    df_tot = pd.merge(df_pv, df_wind, on='Data_Ora', how='inner')
    return df_tot

def esegui_simulazione(df_energia, mult_fv, mult_eolico, carico_w, batteria_wh):
    """Simulazione ibrida: Fotovoltaico + Eolico + Batteria"""
    ore_totali = len(df_energia)
    soc = 0.0  
    ore_scoperte = 0
    energia_tagliata_wh = 0.0      
    energia_fornita_al_carico_wh = 0.0
    tot_fv_prodotto = 0.0
    tot_eolico_prodotto = 0.0
    
    for _, row in df_energia.iterrows():
        # Somma la produzione di entrambe le fonti
        p_fv = row['FV_1kW_W'] * mult_fv
        p_wind = row['Eolico_1kW_W'] * mult_eolico
        p_tot = p_fv + p_wind
        
        tot_fv_prodotto += p_fv
        tot_eolico_prodotto += p_wind
        
        copertura_diretta = min(p_tot, carico_w)
        carico_residuo = carico_w - copertura_diretta
        energia_eccedente = p_tot - copertura_diretta
        energia_ora_corrente = copertura_diretta
        
        if energia_eccedente > 0:
            spazio_batteria = batteria_wh - soc
            energia_immessa = min(energia_eccedente, spazio_batteria)
            soc += energia_immessa
            energia_tagliata_wh += (energia_eccedente - energia_immessa)
            
        if carico_residuo > 0:
            energia_da_batteria = min(soc, carico_residuo)
            soc -= energia_da_batteria
            energia_ora_corrente += energia_da_batteria
            
        if energia_ora_corrente < carico_w:
            ore_scoperte += 1
            
        energia_fornita_al_carico_wh += energia_ora_corrente

    energia_totale_richiesta_wh = carico_w * ore_totali
    energia_totale_prodotta_wh = tot_fv_prodotto + tot_eolico_prodotto
    
    autarchia = (energia_fornita_al_carico_wh / energia_totale_richiesta_wh) * 100
    curtailment = (energia_tagliata_wh / energia_totale_prodotta_wh) * 100 if energia_totale_prodotta_wh > 0 else 0
    
    return {
        "ore_scoperte": ore_scoperte,
        "ore_totali": ore_totali,
        "autarchia": autarchia,
        "curtailment": curtailment,
        "fv_kwh": tot_fv_prodotto / 1000,
        "eolico_kwh": tot_eolico_prodotto / 1000,
        "prodotta_kwh": energia_totale_prodotta_wh / 1000,
        "richiesta_kwh": energia_totale_richiesta_wh / 1000,
        "tagliata_kwh": energia_tagliata_wh / 1000
    }

# ==========================================
# INTERFACCIA STREAMLIT
# ==========================================

st.set_page_config(page_title="Simulatore FV + Eolico", layout="wide")
st.title("🌪️☀️ Simulatore Ibrido Off-Grid (Sole + Vento)")
st.markdown("Combina fotovoltaico ed eolico per vedere come si compensano durante l'anno e riducono l'uso delle batterie.")

if "lat" not in st.session_state:
    st.session_state.lat = 45.4642
if "lon" not in st.session_state:
    st.session_state.lon = 9.1900

col1, col2 = st.columns([1, 1.2])

with col1:
    st.subheader("1. Posizione Geografica")
    m = folium.Map(location=[st.session_state.lat, st.session_state.lon], zoom_start=5)
    folium.Marker([st.session_state.lat, st.session_state.lon], tooltip="Impianto").add_to(m)
    mappa_dati = st_folium(m, height=350, use_container_width=True)
    
    if mappa_dati and mappa_dati.get("last_clicked"):
        st.session_state.lat = mappa_dati["last_clicked"]["lat"]
        st.session_state.lon = mappa_dati["last_clicked"]["lng"]
        st.rerun()
    st.caption(f"Coordinate: Lat {st.session_state.lat:.4f} | Lon {st.session_state.lon:.4f}")

with col2:
    st.subheader("2. Dimensionamento Impianto")
    
    c_fv, c_wind = st.columns(2)
    with c_fv:
        kw_pannelli = st.number_input("Fotovoltaico (kWp):", min_value=0.0, value=5.0, step=1.0)
        tipo_tracker_nome = st.radio("Inseguitore FV:", ["Fisso/Asse Singolo", "Asse Doppio"], horizontal=True)
        tracker = 1 if tipo_tracker_nome == "Fisso/Asse Singolo" else 2
        
    with c_wind:
        kw_eolico = st.number_input("Turbina Eolica (kW):", min_value=0.0, value=2.0, step=1.0)
        st.caption("Usa dati storici del vento a 100m di quota.")
    
    st.markdown("---")
    c_carico, c_batt = st.columns(2)
    with c_carico:
        w_carico = st.number_input("Carico Costante (Watt):", min_value=100.0, value=1000.0, step=100.0)
    with c_batt:
        wh_batteria = st.number_input("Capacità Batteria (Wh):", min_value=0.0, value=20000.0, step=1000.0)
    
    esegui = st.button("🚀 Avvia Simulazione Ibrida", use_container_width=True, type="primary")

st.divider()

# ==========================================
# ESECUZIONE
# ==========================================

if esegui:
    with st.spinner("Scaricamento dati satellite (Sole + Vento) in corso..."):
        df = scarica_profili_energia(st.session_state.lat, st.session_state.lon, tracker)
        
        if df is not None:
            res = esegui_simulazione(df, kw_pannelli, kw_eolico, w_carico, wh_batteria)
            
            st.subheader("📊 Report Energetico Annuale")
            
            m1, m2, m3 = st.columns(3)
            m1.metric("Autarchia (Carico Coperto)", f"{res['autarchia']:.1f}%")
            m2.metric("Curtailment (Energia Buttata)", f"{res['curtailment']:.1f}%")
            m3.metric("Ore di Blackout", f"{res['ore_scoperte']} / {res['ore_totali']}")
            
            st.markdown("---")
            
            m4, m5, m6, m7 = st.columns(4)
            m4.metric("Consumo Richiesto", f"{res['richiesta_kwh']:.0f} kWh")
            m5.metric("Generazione Solare", f"{res['fv_kwh']:.0f} kWh")
            m6.metric("Generazione Eolica", f"{res['eolico_kwh']:.0f} kWh")
            m7.metric("Generazione Totale", f"{res['prodotta_kwh']:.0f} kWh")
            
        else:
            st.error("Errore nello scaricamento dei dati meteo/solari. Riprova con un'altra coordinata.")
