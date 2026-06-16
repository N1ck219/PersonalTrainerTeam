# 📋 Marathon Multi-Agent Personal Trainer - Future Roadmap & TODO

This document compiles future feature suggestions, agent enhancements, and usability upgrades to expand the system.

---

## 1. 💬 Interattività & User Experience (UX)

- [x] **Chat UI Integrata nella Dashboard**
  - Aggiungere un widget di chat in stile "Co-Pilota" in basso a destra per chattare direttamente con il Manager Agent.
  - Consentire di inviare istruzioni in linguaggio naturale per registrare pasti o allenamenti direttamente dal web, senza dipendere da Telegram.
- [x] **Mappe Interattive GPX**
  - Integrare Leaflet.js o Mapbox nella pagina dei dettagli dell'allenamento (`workout_detail.html`) per tracciare la rotta GPS reale registrata nel file GPX.
- [x] **Registro e Mappa degli Infortuni / Dolori**
  - Implementare un selettore visivo del corpo umano per loggare dolori muscolari o articolari localizzati (es. tendine d'Achille sinistro, ginocchio destro) con intensità da 1 a 10.
  - Consentire al Fisiologo di consigliare esercizi di stretching o raccomandare scarico al Trainer.
- [ ] **Visualizzazione 3D delle Tracce GPX**
  - Integrare un visualizzatore 3D (Cesium o Three.js) per riprodurre in modo premium il percorso fatto con sorvolo altimetrico.
- [ ] **Generazione Feed Calendario Sincronizzato (.ics)**
  - Generare un feed iCalendar per abbonarsi al piano allenamenti da Google Calendar o Apple Calendar.
- [ ] **Esportazione PDF dei Report Mensili**
  - Creare un pulsante per esportare un report mensile impaginato splendidamente, con grafici e commenti dei tre agenti per il proprio coach o medico dello sport.

---

## 2. 🤖 Intelligenza degli Agenti & Integrazione Dati

- [ ] **Baseline Fisiologici Dinamici**
  - Sostituire i valori fissi di HRV (65) e RHR (55) con medie mobili reali a 7 e 30 giorni calcolate direttamente dalle metriche Garmin salvate.
  - Adattare la *Readiness* in modo personalizzato basandosi sulle deviazioni standard dell'atleta.
- [x] **Integrazione Meteo API (OpenWeatherMap)**
  - Scaricare i dati meteo (temperatura, umidità, vento) al momento e luogo dell'allenamento.
  - Permettere al Trainer di giustificare cali di prestazione dovuti al caldo estremo o adattare i ritmi delle sessioni future.
- [x] **Analisi di Bio-meccanica ed Efficienza**
  - Incrociare il peso corporeo storico (Garmin Scale) con il passo e la frequenza cardiaca media.
  - Calcolare metriche come il costo energetico stimato e visualizzare i trend di miglioramento dell'efficienza.
- [x] **Calcolo del Caloric Balance (Input vs Output)**
  - Creare un grafico di bilancio calorico confrontando le calorie consumate (da `NutritionLog`) con quelle bruciate (metabolismo basale stimato dal peso + consumo attivo del Garmin GPX).
  - Consentire all'agente Nutrizionista di suggerire strategie di ricarica carboidrati pre-gara e recupero post-allenamento.
- [x] **Modello di Carico Fisiologico CTL / ATL / TSB**
  - Calcolare il Training Stress Score (TSS) per ogni allenamento e stimare Fitness (CTL), Fatica (ATL) e Forma (TSB) per ottimizzare il picco di forma per la gara.
- [ ] **Analisi Avanzata del Sonno e dello Stress Garmin**
  - Analizzare le fasi del sonno e lo stress quotidiano registrati da Garmin per affinare il punteggio di Readiness.
- [x] **Prevenzione Infortuni tramite ACWR (Acute-to-Chronic Workload Ratio)**
  - Calcolare il rapporto tra il carico di allenamento a breve termine (7 giorni) e a lungo termine (28 giorni) per identificare picchi di fatica e segnalare rischi elevati di infortunio.
- [ ] **Analizzatore di Ripetute e Intervalli (Split Analyzer)**
  - Rilevare automaticamente le frazioni veloci e di recupero dagli allenamenti a intervalli per tracciare passo, deriva cardiaca e tempi di recupero reali.
- [x] **Stima del Tasso di Sudorazione e Piani di Idratazione**
  - Integrare i dati meteo ed i pesi pre/post allenamento per calcolare la perdita idrica stimata e pianificare l'idratazione/integrazione per lunghi e maratone.
- [x] **Ottimizzazione Dinamica del Tapering Pre-Gara**
  - Creare un algoritmo per calcolare la riduzione del volume e il mantenimento dell'intensità nelle ultime 3 settimane prima delle maratone programmate.

---

## 3. 👟 Attrezzatura & Sicurezza & Canali Esterni

- [ ] **Notifiche di Usura Scarpe**
  - Mostrare alert visivi sulla Dashboard quando una scarpa supera il 90% (720 km) del limite raccomandato di 800 km.
  - Aggiungere una logica per cui il Trainer raccomanda quale scarpa indossare in base al tipo di allenamento e allo stato delle scarpe attive.
- [x] **Stima dei Tempi di Gara Avanzati**
  - Migliorare la formula di Riegel includendo il trend del volume settimanale e la stima del VO2Max basata sulla FC max reale registrata negli allenamenti più intensi.
- [ ] **Bot Telegram / WhatsApp Bidirezionale**
  - Permettere l'inserimento in linguaggio naturale di pasti (es. tramite l'agente Nutrizionista) e ricevere risposte immediate interpellando il team di agenti.
- [ ] **Programmi di Core Stability e Forza Specifici per Runner**
  - Integrare routine di allenamento per la forza e la prevenzione degli infortuni raccomandate periodicamente dal Trainer e dal Fisiologo.
