# 📋 Marathon Multi-Agent Personal Trainer - Future Roadmap & TODO

This document compiles future feature suggestions, agent enhancements, and usability upgrades to expand the system.

---

## 1. 💬 Interattività & User Experience (UX)

- [ ] **Chat UI Integrata nella Dashboard**
  - Aggiungere un widget di chat in stile "Co-Pilota" in basso a destra per chattare direttamente con il Manager Agent.
  - Consentire di inviare istruzioni in linguaggio naturale per registrare pasti o allenamenti direttamente dal web, senza dipendere da Telegram.
- [ ] **Mappe Interattive GPX**
  - Integrare Leaflet.js o Mapbox nella pagina dei dettagli dell'allenamento (`workout_detail.html`) per tracciare la rotta GPS reale registrata nel file GPX.
- [ ] **Registro e Mappa degli Infortuni / Dolori**
  - Implementare un selettore visivo del corpo umano per loggare dolori muscolari o articolari localizzati (es. tendine d'Achille sinistro, ginocchio destro) con intensità da 1 a 10.
  - Consentire al Fisiologo di consigliare esercizi di stretching o raccomandare scarico al Trainer.

---

## 2. 🤖 Intelligenza degli Agenti & Integrazione Dati

- [ ] **Baseline Fisiologici Dinamici**
  - Sostituire i valori fissi di HRV (65) e RHR (55) con medie mobili reali a 7 e 30 giorni calcolate direttamente dalle metriche Garmin salvate.
  - Adattare la *Readiness* in modo personalizzato basandosi sulle deviazioni standard dell'atleta.
- [ ] **Integrazione Meteo API (OpenWeatherMap)**
  - Scaricare i dati meteo (temperatura, umidità, vento) al momento e luogo dell'allenamento.
  - Permettere al Trainer di giustificare cali di prestazione dovuti al caldo estremo o adattare i ritmi delle sessioni future.
- [ ] **Analisi di Bio-meccanica ed Efficienza**
  - Incrociare il peso corporeo storico (Garmin Scale) con il passo e la frequenza cardiaca media.
  - Calcolare metriche come il costo energetico stimato e visualizzare i trend di miglioramento dell'efficienza.
- [ ] **Calcolo del Caloric Balance (Input vs Output)**
  - Creare un grafico di bilancio calorico confrontando le calorie consumate (da `NutritionLog`) con quelle bruciate (metabolismo basale stimato dal peso + consumo attivo del Garmin GPX).
  - Consentire all'agente Nutrizionista di suggerire strategie di ricarica carboidrati pre-gara e recupero post-allenamento.

---

## 3. 👟 Attrezzatura & Sicurezza

- [ ] **Notifiche di Usura Scarpe**
  - Mostrare alert visivi sulla Dashboard quando una scarpa supera il 90% (720 km) del limite raccomandato di 800 km.
  - Aggiungere una logica per cui il Trainer raccomanda quale scarpa indossare in base al tipo di allenamento e allo stato delle scarpe attive.
- [ ] **Stima dei Tempi di Gara Avanzati**
  - Migliorare la formula di Riegel includendo il trend del volume settimanale e la stima del VO2Max basata sulla FC max reale registrata negli allenamenti più intensi.
