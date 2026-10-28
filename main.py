import streamlit as st
import requests
import pandas as pd
import numpy as np
import folium
from streamlit_folium import st_folium
import altair as alt

# ==========================================
# FUNZIONI DI CALCOLO E DATI
# ==========================================

def calcola_potenza_eolica_mw(v_vento_ms, p_nominale_mw):
    """Calcola la potenza eolica in MW usando una curva di potenza standard"""
    v_cut_in = 3.0    
    v_rated = 11.0    
    v_cut_out = 25.0  
    
    if v_vento_ms < v_cut_in or v_vento_ms > v_cut_out:
        return 0.0
    elif v_cut_in <= v_vento_ms < v_rated:
        # Decadimento quadratico approssimato
        return p_nominale_mw * ((v_vento_ms**2 - v_cut_in**2) / (v_rated**2 - v_cut_in**2))
    else:
        return p_nominale_mw

@st.cache_data(show_spinner=False)
def scarica_vento_openmeteo(lat, lon, anno=2023):
    """Scarica un anno di dati di vento orari da Open-Meteo per un sito"""
    url_wind = "https://archive-api.open-meteo.com/v1/archive"
    params_wind = {
        "latitude": lat, "longitude": lon,
        "start_date": f"{anno}-01-01", 
        "end_date": f"{anno}-12-31",   
        "hourly": "windspeed_100m", "wind_speed_unit": "ms", "timezone": "UTC"
    }
    resp = requests.get(url_wind, params=params_wind)
    if resp.status_code != 200: return None
        
    data = resp.json()
    df = pd.DataFrame({
        'Data_Ora': pd.to_datetime(data['hourly']['time']),
        'Vento_ms': data['hourly']['windspeed_100m']
    })
    return df

@st.cache_data(show_spinner=False)
def genera_carico_sintetico(anno=2023):
    """
    Genera un carico termico residuo fittizio (in MW) per le 6 zone italiane.
    Nella realtà, qui importeresti un CSV di Terna (Dati di Mercato -> Carico).
    """
    date_rng = pd.date_range(start=f'{anno}-01-01', end=f'{anno}-12-31 23:00:00', freq='h')
    ore = len(date_rng)
    
    # Base load fittizio (MW) per zona
    base_load = {'NORD': 15000, 'CNOR': 3000, 'CSUD': 4000, 'SUD': 2500, 'SICI': 2000, 'SARD': 1000}
    
    df_carico = pd.DataFrame({'Data_Ora': date_rng})
    
    for zona, base in base_load.items():
        # Aggiungiamo stagionalità e ciclo giorno/notte
        ciclo_giornaliero = -np.cos(np.linspace(0, 2 * np.pi, 24)) * (base * 0.2)
        ciclo_annuale = np.cos(np.linspace(0, 2 * np.pi, ore)) * (base * 0.15)
        rumore = np.random.normal(0, base * 0.05, ore)
        
        carico_orario = base + np.tile(ciclo_giornaliero, ore // 24 + 1)[:ore] + ciclo_annuale + rumore
        df_carico[zona] = np.maximum(carico_orario, base * 0.4) # Evita carichi negativi
        
    return df_carico

# ==========================================
# INTERFACCIA STREAMLIT
# ==========================================

st.set_page_config(page_title="Simulatore Flotta Eolica Zonale", layout="wide")
st.title("🌬️ Simulatore Macro-Energetico: Impatto dell'Eolico sul Carico Zonale")

# Inizializzazione Session State per la flotta
if 'flotta' not in st.session_state:
    st.session_state.flotta = []
if "map_center" not in st.session_state: 
    st.session_state.map_center = [41.9, 12.5] # Centro Italia

with st.expander("ℹ️ Come funziona questo scenario?"):
    st.markdown("""
    Questo strumento simula l'impatto di un portafoglio di parchi eolici sulla rete elettrica, analizzando le **Zone di Mercato** italiane (NORD, CNOR, CSUD, SUD, SICI, SARD).
    - L'ipotesi di base è **Zero Trasmissione**: l'energia prodotta in una zona può essere consumata solo in quella zona.
    - Il carico di partenza è il **Carico Termico Residuo** (attualmente sintetico, ma sostituibile con dati Terna).
    - Aggiungi parchi eolici sulla mappa, assegnali a una zona e verifica come la **Curva di Durata del Carico** (Load Duration Curve) si abbassa, riducendo l'uso di centrali a gas/carbone.
    """)

# --- LAYOUT SUPERIORE: Mappa e Gestione Flotta ---
col_mappa, col_flotta = st.columns([1.2, 1])

with col_mappa:
    st.subheader("1. Posiziona Parchi Eolici")
    st.caption("Clicca sulla mappa per ottenere le coordinate del nuovo parco.")
    
    m = folium.Map(location=st.session_state.map_center, zoom_start=5)
    
    # Disegna i parchi esistenti
    for i, parco in enumerate(st.session_state.flotta):
        folium.Marker(
            [parco['lat'], parco['lon']], 
            popup=f"{parco['zona']} - {parco['mw']} MW",
            icon=folium.Icon(color="green", icon="leaf")
        ).add_to(m)
        
    mappa = st_folium(m, height=400, use_container_width=True)

with col_flotta:
    st.subheader("2. Aggiungi al Portafoglio")
    
    # Precompila con l'ultimo click
    lat_click = mappa["last_clicked"]["lat"] if mappa and mappa.get("last_clicked") else 41.9
    lon_click = mappa["last_clicked"]["lng"] if mappa and mappa.get("last_clicked") else 12.5
    
    with st.form("aggiungi_parco"):
        c1, c2 = st.columns(2)
        lat_in = c1.number_input("Latitudine", value=lat_click, format="%.4f")
        lon_in = c2.number_input("Longitudine", value=lon_click, format="%.4f")
        
        c3, c4 = st.columns(2)
        zona_in = c3.selectbox("Zona di Mercato", ["NORD", "CNOR", "CSUD", "SUD", "SICI", "SARD"])
        mw_in = c4.number_input("Capacità Installata (MW)", min_value=1.0, value=50.0, step=10.0)
        
        submit_parco = st.form_submit_button("➕ Aggiungi Parco alla Flotta")
        if submit_parco:
            st.session_state.flotta.append({
                "id": len(st.session_state.flotta) + 1,
                "lat": lat_in, "lon": lon_in, 
                "zona": zona_in, "mw": mw_in
            })
            st.rerun()

    st.subheader("La tua Flotta")
    if len(st.session_state.flotta) > 0:
        df_flotta = pd.DataFrame(st.session_state.flotta)
        st.dataframe(df_flotta[['id', 'zona', 'mw', 'lat', 'lon']], hide_index=True, use_container_width=True)
        if st.button("🗑️ Svuota Flotta"):
            st.session_state.flotta = []
            st.rerun()
    else:
        st.info("Nessun parco aggiunto. Clicca sulla mappa e aggiungi siti.")

st.divider()

# --- MOTORE DI SIMULAZIONE ---
if st.button("🚀 Avvia Simulazione di Rete (Anno 2023)", type="primary", use_container_width=True):
    if len(st.session_state.flotta) == 0:
        st.warning("Aggiungi almeno un parco eolico alla flotta prima di simulare.")
    else:
        with st.spinner("Scaricamento profili di vento e calcolo dispacciamento orario in corso..."):
            # 1. Carica il carico di base
            df_carico = genera_carico_sintetico(2023)
            
            # 2. Prepara un DataFrame per la produzione eolica aggregata per zona
            df_prod_zone = pd.DataFrame({'Data_Ora': df_carico['Data_Ora']})
            for zona in ["NORD", "CNOR", "CSUD", "SUD", "SICI", "SARD"]:
                df_prod_zone[zona] = 0.0
            
            # 3. Scarica dati e calcola produzione per ogni parco
            for parco in st.session_state.flotta:
                df_vento = scarica_vento_openmeteo(parco['lat'], parco['lon'], 2023)
                if df_vento is not None:
                    # Calcola i MW orari e aggiungili al totale della zona corrispondente
                    prod_oraria = df_vento['Vento_ms'].apply(lambda v: calcola_potenza_eolica_mw(v, parco['mw']))
                    df_prod_zone[parco['zona']] += prod_oraria.values

            # 4. Calcola i bilanci zonali (Zero Trasmissione)
            risultati_zonali = []
            
            # Creiamo i tab per visualizzare i risultati zona per zona
            zone_attive = df_flotta['zona'].unique().tolist()
            tabs = st.tabs(zone_attive + ["Riepilogo Nazionale"])
            
            df_nazionale_carico = 0
            df_nazionale_residuo = 0
            
            for idx, zona in enumerate(zone_attive):
                carico_zona = df_carico[zona]
                eolico_zona = df_prod_zone[zona]
                
                # Il carico residuo non può scendere sotto zero (l'eccesso è curtailment/overgeneration)
                carico_residuo = np.maximum(carico_zona - eolico_zona, 0)
                curtailment = np.maximum(eolico_zona - carico_zona, 0)
                
                # Statistiche
                energia_richiesta_gwh = carico_zona.sum() / 1000
                energia_eolica_gwh = eolico_zona.sum() / 1000
                energia_termica_residua_gwh = carico_residuo.sum() / 1000
                energia_tagliata_gwh = curtailment.sum() / 1000
                copertura_pct = ((energia_richiesta_gwh - energia_termica_residua_gwh) / energia_richiesta_gwh) * 100
                
                # Aggiorna totali nazionali
                df_nazionale_carico += carico_zona
                df_nazionale_residuo += carico_residuo
                
                # --- VISUALIZZAZIONE ZONALE ---
                with tabs[idx]:
                    st.subheader(f"Zona {zona} - Risultati 2023")
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("Capacità Eolica (MW)", df_flotta[df_flotta['zona']==zona]['mw'].sum())
                    m2.metric("Copertura del Carico", f"{copertura_pct:.1f} %")
                    m3.metric("Energia Eolica Prodotta", f"{energia_eolica_gwh:.0f} GWh")
                    m4.metric("Overgeneration (Sprecata)", f"{energia_tagliata_gwh:.0f} GWh")
                    
                    # Grafico: Load Duration Curve (Curva di durata del carico)
                    st.markdown("#### Curva di Durata del Carico (Load Duration Curve)")
                    st.caption("Ordina tutte le 8760 ore dell'anno dal carico più alto al più basso. L'area blu mostra il lavoro risparmiato alle centrali termiche.")
                    
                    carico_ordinato = np.sort(carico_zona)[::-1]
                    residuo_ordinato = np.sort(carico_residuo)[::-1]
                    
                    df_ldc = pd.DataFrame({
                        'Ore (da 1 a 8760)': np.arange(1, 8761),
                        'Carico Iniziale (MW)': carico_ordinato,
                        'Carico Residuo (MW)': residuo_ordinato
                    }).melt('Ore (da 1 a 8760)', var_name='Tipo', value_name='MW')
                    
                    chart_ldc = alt.Chart(df_ldc).mark_line().encode(
                        x='Ore (da 1 a 8760)',
                        y='MW:Q',
                        color=alt.Color('Tipo:N', scale=alt.Scale(domain=['Carico Iniziale (MW)', 'Carico Residuo (MW)'], range=['#CCCCCC', '#FF3333']))
                    ).properties(height=350).interactive()
                    
                    st.altair_chart(chart_ldc, use_container_width=True)

            # --- RIEPILOGO NAZIONALE ---
            with tabs[-1]:
                st.subheader("Impatto Aggregato di Sistema")
                st.info("Nota: nell'ipotesi di 'Zero Trasmissione', questi numeri sono la pura somma aritmetica delle singole zone. L'energia sprecata al Sud non può aiutare il deficit del Nord.")
                
                carico_tot = df_nazionale_carico.sum() / 1000
                residuo_tot = df_nazionale_residuo.sum() / 1000
                risparmio_gas = (carico_tot - residuo_tot) * 0.20 # Assumendo circa 0.20 smc di gas per kWh termoelettrico residuo
                
                c1, c2, c3 = st.columns(3)
                c1.metric("Totale Carico Ridotto (GWh)", f"{(carico_tot - residuo_tot):.0f}")
                c2.metric("Risparmio stimato Gas (Mln Smc)", f"{risparmio_gas/1000:.0f}")
                c3.metric("Capacità Totale Flotta (MW)", df_flotta['mw'].sum())
