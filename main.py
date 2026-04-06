import streamlit as st
import requests
import pandas as pd
import folium
from streamlit_folium import st_folium

# ==========================================
# FUNZIONI DI CALCOLO (Con Caching)
# ==========================================

@st.cache_data(show_spinner=False)
def scarica_profilo_pvgis(lat, lon, tipo_tracker):
    """Scarica i dati da PVGIS. La cache evita di riscaricare gli stessi dati se cambi solo la batteria."""
    url = "https://re.jrc.ec.europa.eu/api/v5_2/seriescalc"
    params = {
        "lat": lat, "lon": lon, "pvcalculation": 1,
        "peakpower": 1.0, "loss": 14.0, "trackingtype": tipo_tracker, 
        "outputformat": "json", "startyear": 2020, "endyear": 2020
    }
    response = requests.get(url, params=params)
    if response.status_code == 200:
        data = response.json()
        df = pd.DataFrame(data['outputs']['hourly'])
        df['time'] = pd.to_datetime(df['time'], format='%Y%m%d:%H%M')
        df = df[['time', 'P']]
        df.rename(columns={'time': 'Data_Ora', 'P': 'Produzione_W'}, inplace=True)
        return df
    return None

def esegui_simulazione(df_produzione, moltiplicatore_fv, carico_w, batteria_wh):
    """Esegue la simulazione ora per ora"""
    produzione_base = df_produzione['Produzione_W'].values
    ore_totali = len(produzione_base)
    
    soc = 0.0  
    ore_scoperte = 0
    energia_tagliata_wh = 0.0      
    energia_fornita_al_carico_wh = 0.0
    energia_totale_prodotta_wh = 0.0
    
    for p_fv_base in produzione_base:
        p_fv = p_fv_base * moltiplicatore_fv
        energia_totale_prodotta_wh += p_fv
        
        copertura_diretta = min(p_fv, carico_w)
        carico_residuo = carico_w - copertura_diretta
        energia_eccedente = p_fv - copertura_diretta
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
    autarchia = (energia_fornita_al_carico_wh / energia_totale_richiesta_wh) * 100
    curtailment = (energia_tagliata_wh / energia_totale_prodotta_wh) * 100 if energia_totale_prodotta_wh > 0 else 0
    
    return {
        "ore_scoperte": ore_scoperte,
        "ore_totali": ore_totali,
        "autarchia": autarchia,
        "curtailment": curtailment,
        "richiesta_kwh": energia_totale_richiesta_wh / 1000,
        "prodotta_kwh": energia_totale_prodotta_wh / 1000,
        "tagliata_kwh": energia_tagliata_wh / 1000
    }

# ==========================================
# INTERFACCIA STREAMLIT
# ==========================================

st.set_page_config(page_title="Simulatore Off-Grid", layout="wide")
st.title("☀️ Simulatore Sopravvivenza Off-Grid")
st.markdown("Scopri quanta potenza fotovoltaica e batteria servono per staccarti davvero dalla rete.")

# Inizializza session state per le coordinate della mappa
if "lat" not in st.session_state:
    st.session_state.lat = 45.4642
if "lon" not in st.session_state:
    st.session_state.lon = 9.1900

col1, col2 = st.columns([1, 1])

with col1:
    st.subheader("1. Scegli la Posizione")
    st.caption("Clicca sulla mappa per spostare l'impianto.")
    
    # Crea mappa folium
    m = folium.Map(location=[st.session_state.lat, st.session_state.lon], zoom_start=5)
    folium.Marker([st.session_state.lat, st.session_state.lon], tooltip="Impianto").add_to(m)
    
    # Renderizza mappa interattiva
    mappa_dati = st_folium(m, height=350, use_container_width=True)
    
    # Aggiorna coordinate se l'utente clicca sulla mappa
    if mappa_dati and mappa_dati.get("last_clicked"):
        st.session_state.lat = mappa_dati["last_clicked"]["lat"]
        st.session_state.lon = mappa_dati["last_clicked"]["lng"]
        st.rerun()

    st.write(f"**Coordinate attuali:** Lat: {st.session_state.lat:.4f} | Lon: {st.session_state.lon:.4f}")

with col2:
    st.subheader("2. Imposta i Parametri")
    
    tipo_tracker_nome = st.radio("Inseguitore Solare:", ["Asse Singolo", "Asse Doppio"], horizontal=True)
    tracker = 1 if tipo_tracker_nome == "Asse Singolo" else 2
    
    kw_pannelli = st.number_input("Potenza Fotovoltaico (kWp):", min_value=1.0, value=5.0, step=1.0)
    w_carico = st.number_input("Carico Costante (Watt):", min_value=100.0, value=1000.0, step=100.0)
    wh_batteria = st.number_input("Capacità Batteria (Wh):", min_value=1000.0, value=20000.0, step=1000.0)
    
    esegui = st.button("🚀 Avvia Simulazione Annuale", use_container_width=True, type="primary")

st.divider()

# ==========================================
# ESECUZIONE E RISULTATI
# ==========================================

if esegui:
    with st.spinner("Scaricamento dati e simulazione in corso..."):
        df = scarica_profilo_pvgis(st.session_state.lat, st.session_state.lon, tracker)
        
        if df is not None:
            res = esegui_simulazione(df, kw_pannelli, w_carico, wh_batteria)
            
            st.subheader("📊 Report di Sistema")
            
            # Mostriamo i KPI principali con le metriche eleganti di Streamlit
            m1, m2, m3 = st.columns(3)
            m1.metric("Autarchia (Carico Coperto)", f"{res['autarchia']:.1f}%")
            m2.metric("Curtailment (Energia Sprecata)", f"{res['curtailment']:.1f}%")
            m3.metric("Ore di Blackout", f"{res['ore_scoperte']} / {res['ore_totali']}")
            
            st.markdown("---")
            
            m4, m5, m6 = st.columns(3)
            m4.metric("Energia Necessaria", f"{res['richiesta_kwh']:.0f} kWh")
            m5.metric("Energia Prodotta", f"{res['prodotta_kwh']:.0f} kWh")
            m6.metric("Energia Buttata", f"{res['tagliata_kwh']:.0f} kWh")
            
            # Un piccolo alert se il blackout è troppo alto
            if res['autarchia'] < 95:
                st.warning(f"⚠️ Il sistema va in blackout per {res['ore_scoperte']} ore all'anno. Aumenta la batteria o i pannelli, ma tieni d'occhio l'energia sprecata d'estate!")
            elif res['curtailment'] > 30:
                st.info("💡 Sei indipendente, ma stai sovradimensionando molto: d'estate butti via tantissima energia perché la batteria è sempre piena.")
            else:
                st.success("✅ Ottimo bilanciamento del sistema!")
                
        else:
            st.error("Errore nello scaricamento dei dati. Hai cliccato in mezzo al mare?")
