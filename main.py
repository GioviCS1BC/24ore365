import streamlit as st
import requests
import pandas as pd
import folium
from streamlit_folium import st_folium

# ==========================================
# FUNZIONI DI CALCOLO
# ==========================================

def calcola_potenza_eolica(v_vento_ms, p_nominale_w=1000.0):
    v_cut_in = 3.0    
    v_rated = 12.0    
    v_cut_out = 25.0  
    
    if v_vento_ms < v_cut_in or v_vento_ms > v_cut_out:
        return 0.0
    elif v_cut_in <= v_vento_ms < v_rated:
        return p_nominale_w * ((v_vento_ms**3 - v_cut_in**3) / (v_rated**3 - v_cut_in**3))
    else:
        return p_nominale_w

@st.cache_data(show_spinner=False)
def scarica_profili_energia(lat, lon, tipo_tracker):
    # 1. FOTOVOLTAICO (PVGIS)
    url_pv = "https://re.jrc.ec.europa.eu/api/v5_2/seriescalc"
    params_pv = {
        "lat": lat, "lon": lon, "pvcalculation": 1,
        "peakpower": 1.0, "loss": 14.0, "trackingtype": tipo_tracker, 
        "outputformat": "json", "startyear": 2020, "endyear": 2020
    }
    resp_pv = requests.get(url_pv, params=params_pv)
    if resp_pv.status_code != 200: return None
    
    data_pv = resp_pv.json()
    df_pv = pd.DataFrame(data_pv['outputs']['hourly'])
    df_pv['Data_Ora'] = pd.to_datetime(df_pv['time'], format='%Y%m%d:%H%M').dt.floor('h')
    df_pv.rename(columns={'P': 'FV_1kW_W'}, inplace=True)
    df_pv = df_pv[['Data_Ora', 'FV_1kW_W']]
    
    # 2. VENTO (Open-Meteo)
    url_wind = "https://archive-api.open-meteo.com/v1/archive"
    params_wind = {
        "latitude": lat, "longitude": lon,
        "start_date": "2020-01-01", "end_date": "2020-12-31",
        "hourly": "windspeed_100m", "wind_speed_unit": "ms", "timezone": "UTC"
    }
    resp_wind = requests.get(url_wind, params=params_wind)
    if resp_wind.status_code != 200: return None
        
    data_wind = resp_wind.json()
    df_wind = pd.DataFrame({
        'Data_Ora': pd.to_datetime(data_wind['hourly']['time']).floor('h'),
        'Vento_ms': data_wind['hourly']['windspeed_100m']
    })
    df_wind['Eolico_1kW_W'] = df_wind['Vento_ms'].apply(calcola_potenza_eolica)
    df_wind = df_wind[['Data_Ora', 'Eolico_1kW_W']]
    
    return pd.merge(df_pv, df_wind, on='Data_Ora', how='inner')

def esegui_simulazione(df_energia, mult_fv, mult_eolico, carico_w, batteria_wh, p_backup_w):
    ore_totali = len(df_energia)
    soc = 0.0  
    ore_backup = 0
    ore_blackout = 0
    energia_tagliata_wh = 0.0      
    energia_backup_wh = 0.0
    energia_fornita_rinnovabili_wh = 0.0
    tot_fv_wh = 0.0
    tot_eolico_wh = 0.0
    storia_soc = []
    
    for _, row in df_energia.iterrows():
        p_fv = row['FV_1kW_W'] * mult_fv
        p_wind = row['Eolico_1kW_W'] * mult_eolico
        p_rinnovabile = p_fv + p_wind
        tot_fv_wh += p_fv
        tot_eolico_wh += p_wind
        
        # 1. Copertura diretta
        copertura_diretta = min(p_rinnovabile, carico_w)
        carico_residuo = carico_w - copertura_diretta
        energia_eccedente = p_rinnovabile - copertura_diretta
        energia_da_rinnovabili_ora = copertura_diretta
        
        # 2. Carica batteria
        if energia_eccedente > 0:
            spazio = batteria_wh - soc
            energia_immessa = min(energia_eccedente, spazio)
            soc += energia_immessa
            energia_tagliata_wh += (energia_eccedente - energia_immessa)
            
        # 3. Scarica batteria
        if carico_residuo > 0:
            prelievo_batt = min(soc, carico_residuo)
            soc -= prelievo_batt
            carico_residuo -= prelievo_batt
            energia_da_rinnovabili_ora += prelievo_batt
            
        # 4. Intervento Generatore di Backup
        energia_fornita_backup_ora = 0.0
        if carico_residuo > 0 and p_backup_w > 0:
            ore_backup += 1
            energia_fornita_backup_ora = min(p_backup_w, carico_residuo)
            energia_backup_wh += energia_fornita_backup_ora
            carico_residuo -= energia_fornita_backup_ora
            
        # 5. Blackout residuo (se neanche il generatore basta)
        if carico_residuo > 0.01: # Tolleranza float
            ore_blackout += 1
            
        energia_fornita_rinnovabili_wh += energia_da_rinnovabili_ora
        storia_soc.append(soc)

    richiesta_totale_wh = carico_w * ore_totali
    autarchia_rinnovabile = (energia_fornita_rinnovabili_wh / richiesta_totale_wh) * 100 if richiesta_totale_wh > 0 else 100.0
    copertura_totale = ((energia_fornita_rinnovabili_wh + energia_backup_wh) / richiesta_totale_wh) * 100 if richiesta_totale_wh > 0 else 100.0
    curtailment = (energia_tagliata_wh / (tot_fv_wh + tot_eolico_wh)) * 100 if (tot_fv_wh + tot_eolico_wh) > 0 else 0.0
    
    return {
        "ore_backup": ore_backup,
        "ore_blackout": ore_blackout,
        "autarchia_rinnovabile": autarchia_rinnovabile,
        "copertura_totale": copertura_totale,
        "curtailment": curtailment,
        "backup_kwh": energia_backup_wh / 1000,
        "fv_kwh": tot_fv_wh / 1000,
        "eolico_kwh": tot_eolico_wh / 1000,
        "richiesta_kwh": richiesta_totale_wh / 1000,
        "tagliata_kwh": energia_tagliata_wh / 1000,  # <--- ECCO LA RIGA MANCANTE
        "storia_soc": storia_soc
    }

# ==========================================
# INTERFACCIA STREAMLIT
# ==========================================

st.set_page_config(page_title="Simulatore Ibrido con Backup", layout="wide")
st.title("🔋 Simulatore Ibrido: Rinnovabili + Batteria + Backup")

with st.expander("ℹ️ Come funziona il Generatore di Backup?"):
    st.markdown("""
    In questa versione, il sistema non va subito in blackout quando la batteria è vuota.
    1. Il sistema usa prima **Sole e Vento**.
    2. Se non bastano, attinge dalla **Batteria**.
    3. Se la batteria è vuota, si accende il **Generatore di Backup** (fino alla potenza massima impostata).
    4. Se il carico richiesto è superiore alla potenza del generatore, si verifica un **Blackout parziale**.
    """)

if "lat" not in st.session_state: st.session_state.lat, st.session_state.lon = 45.4642, 9.1900

col1, col2 = st.columns([1, 1.2])

with col1:
    st.subheader("1. Posizione")
    m = folium.Map(location=[st.session_state.lat, st.session_state.lon], zoom_start=5)
    folium.Marker([st.session_state.lat, st.session_state.lon]).add_to(m)
    mappa = st_folium(m, height=300, use_container_width=True)
    if mappa and mappa.get("last_clicked"):
        st.session_state.lat, st.session_state.lon = mappa["last_clicked"]["lat"], mappa["last_clicked"]["lng"]
        st.rerun()

with col2:
    st.subheader("2. Parametri Impianto")
    c1, c2 = st.columns(2)
    kw_fv = c1.number_input("Fotovoltaico (kWp):", 0.0, 100.0, 5.0)
    kw_wind = c2.number_input("Eolico (kW):", 0.0, 100.0, 2.0)
    kw_backup = c1.number_input("Generatore Backup (kW):", 0.0, 100.0, 1.0, help="Potenza costante del generatore di emergenza")
    kwh_batt = c2.number_input("Batteria (kWh):", 0.0, 500.0, 20.0)
    mwh_annui = st.number_input("Fabbisogno Annuo (MWh):", 0.0, 100.0, 8.76)
    esegui = st.button("🚀 Avvia Simulazione", use_container_width=True, type="primary")

st.divider()

if esegui:
    with st.spinner("Calcolo in corso..."):
        df = scarica_profili_energia(st.session_state.lat, st.session_state.lon, 1)
        if df is not None:
            w_carico = (mwh_annui * 1_000_000) / len(df)
            res = esegui_simulazione(df, kw_fv, kw_wind, w_carico, kwh_batt*1000, kw_backup*1000)
            
            # --- METRICHE ---
            st.subheader("📊 Risultati Annuali")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Autarchia Rinnovabile", f"{res['autarchia_rinnovabile']:.1f}%", help="Quota di carico coperta solo da sole, vento e batteria")
            m2.metric("Copertura Totale", f"{res['copertura_totale']:.1f}%", help="Quota coperta includendo il generatore di backup")
            m3.metric("Accensioni Backup", f"{res['ore_backup']} ore")
            m4.metric("Blackout Residui", f"{res['ore_blackout']} ore")
            
            st.markdown("---")
            c1, c2, c3 = st.columns(3)
            c1.write(f"**Produzione Rinnovabile:** {res['fv_kwh'] + res['eolico_kwh']:.0f} kWh")
            c2.write(f"**Energia da Backup:** {res['backup_kwh']:.1f} kWh")
            c3.write(f"**Energia Sprecata (Curtailment):** {res['tagliata_kwh']:.0f} kWh")
            
            # --- GRAFICO ---
            st.subheader("🔋 Stato della Batteria (kWh)")
            df_plot = pd.DataFrame({"Data": df["Data_Ora"], "SOC": [v/1000 for v in res["storia_soc"]]}).set_index("Data")
            st.line_chart(df_plot)
            
            if res['ore_blackout'] > 0:
                st.error(f"⚠️ Attenzione: Nonostante il generatore da {kw_backup}kW, ci sono ancora {res['ore_blackout']} ore di blackout perché il carico di picco supera la potenza del backup.")
