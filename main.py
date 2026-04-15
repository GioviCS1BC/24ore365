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
    # 1. FOTOVOLTAICO (PVGIS) - 2017-2020
    url_pv = "https://re.jrc.ec.europa.eu/api/v5_2/seriescalc"
    params_pv = {
        "lat": lat, 
        "lon": lon, 
        "pvcalculation": 1,
        "peakpower": 1.0, 
        "loss": 14.0,  
        "outputformat": "json", 
        "startyear": 2017, 
        "endyear": 2020    
    }
    
    if tipo_tracker == 0:
        params_pv["trackingtype"] = 0
        params_pv["optimalangles"] = 1
    elif tipo_tracker == 5:
        params_pv["trackingtype"] = 5
        params_pv["optimalangles"] = 1 
    else:
        params_pv["trackingtype"] = tipo_tracker
        
    resp_pv = requests.get(url_pv, params=params_pv)
    if resp_pv.status_code != 200: return None
    
    data_pv = resp_pv.json()
    df_pv = pd.DataFrame(data_pv['outputs']['hourly'])
    df_pv['Data_Ora'] = pd.to_datetime(df_pv['time'], format='%Y%m%d:%H%M').dt.floor('h')
    df_pv.rename(columns={'P': 'FV_1kW_W'}, inplace=True)
    df_pv = df_pv[['Data_Ora', 'FV_1kW_W']]
    
    # 2. VENTO (Open-Meteo) - 2017-2020
    url_wind = "https://archive-api.open-meteo.com/v1/archive"
    params_wind = {
        "latitude": lat, "longitude": lon,
        "start_date": "2017-01-01", 
        "end_date": "2020-12-31",   
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

def esegui_simulazione(df_energia, mult_fv, mult_eolico, carico_w, batteria_wh, p_backup_w, perc_carico_fondamentale):
    ore_totali = len(df_energia)
    soc = 0.0  
    ore_backup = 0
    ore_blackout = 0
    ore_riduzione_carico = 0
    energia_tagliata_wh = 0.0      
    energia_backup_wh = 0.0
    energia_fornita_rinnovabili_wh = 0.0
    tot_fv_wh = 0.0
    tot_eolico_wh = 0.0
    richiesta_effettiva_totale_wh = 0.0
    
    # Lista per il grafico della percentuale
    storia_perc_rinnovabili = []
    
    for _, row in df_energia.iterrows():
        p_fv = row['FV_1kW_W'] * mult_fv
        p_wind = row['Eolico_1kW_W'] * mult_eolico
        p_rinnovabile = p_fv + p_wind
        
        tot_fv_wh += p_fv
        tot_eolico_wh += p_wind
        
        # --- GESTIONE CARICO FONDAMENTALE ---
        carico_effettivo_w = carico_w
        capacita_disponibile = p_rinnovabile + soc + p_backup_w
        
        # Se l'energia non basta per tutto il carico e abbiamo impostato un limite di emergenza
        if capacita_disponibile < carico_w and perc_carico_fondamentale < 100.0:
            carico_effettivo_w = carico_w * (perc_carico_fondamentale / 100.0)
            ore_riduzione_carico += 1
            
        richiesta_effettiva_totale_wh += carico_effettivo_w
        
        # 1. Quanta energia rinnovabile usiamo direttamente?
        copertura_diretta = min(p_rinnovabile, carico_effettivo_w)
        carico_residuo = carico_effettivo_w - copertura_diretta
        energia_eccedente = p_rinnovabile - copertura_diretta
        energia_da_rinnovabili_ora = copertura_diretta
            
        # 2. Carica della batteria
        if energia_eccedente > 0:
            spazio = batteria_wh - soc
            energia_immessa = min(energia_eccedente, spazio)
            soc += energia_immessa
            energia_tagliata_wh += (energia_eccedente - energia_immessa)
            
        # 3. Prelievo dalla batteria
        batt_usata_ora = 0.0
        if carico_residuo > 0:
            batt_usata_ora = min(soc, carico_residuo)
            soc -= batt_usata_ora
            carico_residuo -= batt_usata_ora
            energia_da_rinnovabili_ora += batt_usata_ora
            
        # 4. Generatore di Backup
        gen_usato_ora = 0.0
        if carico_residuo > 0 and p_backup_w > 0:
            ore_backup += 1
            gen_usato_ora = min(p_backup_w, carico_residuo)
            energia_backup_wh += gen_usato_ora
            carico_residuo -= gen_usato_ora
            
        # 5. Blackout finale
        if carico_residuo > 0.01:
            ore_blackout += 1
            
        energia_fornita_rinnovabili_wh += energia_da_rinnovabili_ora
        
        # Calcolo della percentuale di rinnovabili in quest'ora
        perc_rinnovabile = (energia_da_rinnovabili_ora / carico_effettivo_w) * 100 if carico_effettivo_w > 0 else 100.0
        storia_perc_rinnovabili.append(perc_rinnovabile)

    autarchia_rinnovabile = (energia_fornita_rinnovabili_wh / richiesta_effettiva_totale_wh) * 100 if richiesta_effettiva_totale_wh > 0 else 100.0
    copertura_totale = ((energia_fornita_rinnovabili_wh + energia_backup_wh) / richiesta_effettiva_totale_wh) * 100 if richiesta_effettiva_totale_wh > 0 else 100.0
    curtailment = (energia_tagliata_wh / (tot_fv_wh + tot_eolico_wh)) * 100 if (tot_fv_wh + tot_eolico_wh) > 0 else 0.0
    
    return {
        "ore_backup": ore_backup,
        "ore_blackout": ore_blackout,
        "ore_riduzione_carico": ore_riduzione_carico,
        "autarchia_rinnovabile": autarchia_rinnovabile,
        "copertura_totale": copertura_totale,
        "curtailment": curtailment,
        "backup_kwh": energia_backup_wh / 1000,
        "fv_kwh": tot_fv_wh / 1000,
        "eolico_kwh": tot_eolico_wh / 1000,
        "richiesta_kwh": richiesta_effettiva_totale_wh / 1000,
        "tagliata_kwh": energia_tagliata_wh / 1000,
        "storia_perc_rinnovabili": storia_perc_rinnovabili
    }

# ==========================================
# INTERFACCIA STREAMLIT
# ==========================================

st.set_page_config(page_title="Simulatore Ibrido con Backup", layout="wide")
st.title("🔋 Simulatore Ibrido: Rinnovabili + Batteria + Backup")

with st.expander("ℹ️ Come funziona questo simulatore?"):
    st.markdown("""
    **Come funziona questo simulatore?**
    
    Questo strumento avanzato simula il bilancio energetico orario di un impianto ibrido completamente off-grid (staccato dalla rete). 
    A differenza delle simulazioni classiche su un singolo anno, questo modello elabora **4 anni consecutivi (circa 35.000 ore dal 2017 al 2020)** per testare la tenuta del sistema anche di fronte ad annate metereologicamente "sfortunate" e inverni particolarmente rigidi. 
    I risultati finali sono poi calcolati come media annua per una facile lettura.

    Il motore di calcolo analizza l'interazione tra fonti rinnovabili, accumulo e back-up seguendo questa rigida logica di priorità:

    1. **Autoconsumo Diretto:** Il sistema dà sempre la priorità all'uso diretto dell'energia prodotta in quell'istante da **Sole e Vento** per alimentare i consumi.
    2. **Accumulo e Curtailment:** Se la produzione rinnovabile supera la domanda, l'energia eccedente carica la **Batteria**. Quando la batteria raggiunge il 100%, l'ulteriore energia prodotta non può essere stoccata e viene inevitabilmente "scartata" o sprecata (fenomeno noto come *Curtailment*).
    3. **Prelievo dall'Accumulo:** Quando la produzione da sole e vento cala (es. di notte o in assenza di vento), il sistema attinge l'energia mancante dalla Batteria.
    4. **Riduzione Carico (Load Shedding):** Se l'energia disponibile (rinnovabili + batteria + generatore) è inferiore alla domanda totale, il sistema disattiva i carichi non prioritari riducendo la domanda alla sola quota del **Carico Fondamentale**.
    5. **Intervento del Generatore:** Se la batteria si svuota completamente, si accende il **Generatore di Backup** come "scialuppa di salvataggio".
    6. **Blackout:** Se il carico richiesto (o il carico fondamentale) supera l'energia che batteria e generatore insieme possono fornire, si verifica un **Blackout**.

    ---

    ✉️ **Contatti**
    Per info, suggerimenti o per discutere dell'impatto climatico ed energetico di queste simulazioni, puoi scrivermi a: **giovanni@unbelclima.it**
    """)

if "lat" not in st.session_state: st.session_state.lat, st.session_state.lon = 45.4642, 9.1900

col1, col2 = st.columns([1, 1.2])

with col1:
    st.subheader("1. Posizione Geografica")
    m = folium.Map(location=[st.session_state.lat, st.session_state.lon], zoom_start=5)
    folium.Marker([st.session_state.lat, st.session_state.lon]).add_to(m)
    mappa = st_folium(m, height=350, use_container_width=True)
    if mappa and mappa.get("last_clicked"):
        st.session_state.lat, st.session_state.lon = mappa["last_clicked"]["lat"], mappa["last_clicked"]["lng"]
        st.rerun()

with col2:
    st.subheader("2. Parametri Impianto")
    
    tipo_tracker_nome = st.selectbox("Tipologia Fotovoltaico:", [
        "Fisso (Sud Ottimizzato)", 
        "Insegue Inclinazione (Nord-Sud / Asse Est-Ovest)",
        "Insegue Est-Ovest (Asse Nord-Sud Inclinato)", 
        "Asse Doppio (Inseguitore Totale)"
    ])
    
    if tipo_tracker_nome == "Fisso (Sud Ottimizzato)":
        tracker = 0
    elif tipo_tracker_nome == "Insegue Inclinazione (Nord-Sud / Asse Est-Ovest)":
        tracker = 4
    elif tipo_tracker_nome == "Insegue Est-Ovest (Asse Nord-Sud Inclinato)":
        tracker = 5
    else:
        tracker = 2
        
    c1, c2 = st.columns(2)
    with c1:
        kw_fv = st.number_input("Fotovoltaico (kWp):", 0.0, 100.0, 5.0)
        kw_backup = st.number_input("Generatore Backup (kW):", 0.0, 100.0, 1.0)
        
    with c2:
        kw_wind = st.number_input("Eolico (kW):", 0.0, 100.0, 2.0)
        kwh_batt = st.number_input("Batteria (kWh):", 0.0, 500.0, 20.0)
        
    st.markdown("---")
    mwh_annui = st.number_input("Fabbisogno Annuo (MWh):", 0.0, 100.0, 8.76)
    st.caption(f"💡 Equivale a un carico costante di **{(mwh_annui * 1000) / 8760:.2f} kW**")
    
    # NUOVO SLIDER: Carico Fondamentale
    perc_carico_fondamentale = st.slider(
        "Carico Fondamentale / Emergenza (%)", 
        min_value=10, max_value=100, value=100, step=5
    )
    st.caption("Se l'energia scarseggia, il sistema 'stacca' le utenze non essenziali riducendo la domanda a questa percentuale per evitare blackout totali.")

st.divider()
st.subheader("💰 Stima Investimento Iniziale (CAPEX)")

costo_fv = kw_fv * 500.0
costo_wind = kw_wind * 1000.0
costo_batt = kwh_batt * 80.0
costo_totale = costo_fv + costo_wind + costo_batt

def format_euro(cifra):
    return f"€ {cifra:,.0f}".replace(",", ".")

col_c1, col_c2, col_c3, col_c4 = st.columns(4)
col_c1.metric("Pannelli FV (500 €/kW)", format_euro(costo_fv))
col_c2.metric("Turbina Eolica (1000 €/kW)", format_euro(costo_wind))
col_c3.metric("Batterie (80 €/kWh)", format_euro(costo_batt))
col_c4.metric("TOTALE IMPIANTO", format_euro(costo_totale))

st.caption("*Nota: Il costo del generatore di backup, degli inverter, del cablaggio e dell'installazione non sono inclusi in questa stima.*")
    
st.markdown("<br>", unsafe_allow_html=True)
esegui = st.button("🚀 Avvia Simulazione Energetica (Analisi 4 Anni)", use_container_width=True, type="primary")

st.divider()

if esegui:
    with st.spinner("Scaricamento dati 2017-2020 e simulazione di 35.000 ore in corso..."):
        df = scarica_profili_energia(st.session_state.lat, st.session_state.lon, tracker)
        if df is not None:
            anni_simulati = len(df) / 8760.0
            w_carico = (mwh_annui * 1_000_000) / (len(df) / anni_simulati) 
            
            res = esegui_simulazione(
                df, kw_fv, kw_wind, w_carico, kwh_batt*1000, kw_backup*1000, perc_carico_fondamentale
            )
            
            st.subheader("📊 Risultati (Valori medi annui basati su un ciclo di 4 anni)")
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Autarchia Rinnovabile", f"{res['autarchia_rinnovabile']:.1f}%")
            m2.metric("Copertura Totale", f"{res['copertura_totale']:.1f}%")
            m3.metric("Uso Backup", f"{res['ore_backup'] / anni_simulati:.0f} h/anno")
            m4.metric("Taglio Utenze", f"{res['ore_riduzione_carico'] / anni_simulati:.0f} h/anno")
            m5.metric("Blackout Residui", f"{res['ore_blackout'] / anni_simulati:.0f} h/anno")
            
            st.markdown("---")
            c1, c2, c3 = st.columns(3)
            c1.write(f"**Produzione Rinnovabile Media:** {(res['fv_kwh'] + res['eolico_kwh']) / anni_simulati:.0f} kWh/anno")
            c2.write(f"**Energia da Backup Media:** {res['backup_kwh'] / anni_simulati:.1f} kWh/anno")
            c3.write(f"**Energia Sprecata Media:** {res['tagliata_kwh'] / anni_simulati:.0f} kWh/anno")
            
            st.markdown("---")
            st.subheader("📊 Copertura Rinnovabile del Carico")
            st.caption("Questo grafico a linea mostra la percentuale media giornaliera del carico coperta esclusivamente da fonti rinnovabili (Sole, Vento e Batteria). I cali nella linea indicano l'intervento del generatore o momenti di blackout.")
            
            # Creazione del dataframe per il grafico a singola linea
            df_linea = pd.DataFrame({
                "Data": df["Data_Ora"],
                "% Rinnovabili": res["storia_perc_rinnovabili"]
            }).set_index("Data")
            
            # Raggruppiamo i dati facendo la media giornaliera della percentuale per una facile lettura visiva
            df_giornaliero_perc = df_linea.resample('D').mean()
            
            st.line_chart(df_giornaliero_perc, y="% Rinnovabili")
            
            if res['ore_blackout'] > 0:
                st.error(f"⚠️ Attenzione: Il sistema ha subito una media di {res['ore_blackout'] / anni_simulati:.0f} ore di blackout all'anno perché l'energia disponibile non riusciva a coprire nemmeno il carico fondamentale.")
