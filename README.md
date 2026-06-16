# Marathon Multi-Agent Personal Trainer Team

Questo progetto implementa un sistema multi-agente intelligente basato su **LangGraph** e **Gemini (Google GenAI)** progettato per supportare un atleta nella transizione verso la mezza maratona e, successivamente, la maratona completa. Il sistema elabora in modo sinergico dati fisiologici, allenamenti pianificati ed eseguiti (inclusa l'analisi dettagliata di file GPX tramite un segmentatore custom), e informazioni nutrizionali.

---

## 🤖 Il Team di Agenti

Il sistema è guidato da un team di agenti specializzati che collaborano tramite un grafo di stato per analizzare i dati dell'atleta e fornire feedback mirati:

1. **Manager (Router)**: L'agente di coordinamento. Analizza l'input iniziale dell'utente (o l'evento di caricamento di un allenamento) e determina quale esperto del team deve intervenire per primo, gestendo il flusso sequenziale e le deviazioni.
2. **Fisiologo (Physiologist)**: Specializzato nell'analisi dello stato di salute e recupero. Esamina metriche giornaliere come le ore di sonno, l'HRV (variabilità della frequenza cardiaca) e la frequenza cardiaca a riposo (RHR), calcolando uno *Score di Readiness* per determinare il livello di fatica dell'atleta.
3. **Allenatore (Trainer)**: Responsabile della programmazione atletica. Ottimizza e suggerisce i piani di allenamento (corse lente, ripetute, tempo run, lunghi), adatta il carico di lavoro in base alla readiness del fisiologo e monitora la progressione verso le gare target.
4. **Nutrizionista (Nutritionist)**: Cura il piano alimentare e l'idratazione. Calcola il fabbisogno calorico e la ripartizione dei macronutrienti (carboidrati, proteine, grassi) su base giornaliera in base all'allenamento previsto, elaborando anche i pasti registrati dall'atleta.
5. **Risponditore (Responder)**: L'agente finale di sintesi. Raccoglie i report e le raccomandazioni di tutti gli agenti esperti coinvolti nel workflow, assemblando una risposta coerente, motivante e strutturata per l'atleta.

---

## 🛠️ Comandi per lanciare l'applicazione

### 1. Requisiti e Installazione delle Dipendenze
Assicurati di aver configurato il file `.env` con la tua `GEMINI_API_KEY` ed eventuali altre chiavi necessarie.
Successivamente, installa le dipendenze richieste:

```bash
pip install -r requirements.txt
```

### 2. Inizializzazione o Reset del Database
Il database SQLite locale (`marathon_multi_agent.db`) viene creato automaticamente all'avvio dell'applicazione. Se hai bisogno di svuotare le tabelle o ricreare lo schema da zero, puoi eseguire lo script di reset:

```bash
python reset_db.py
```

### 3. Avvio del Server FastAPI (Dashboard e API)
L'applicazione dispone di una dashboard web e di endpoint API per simulare le interazioni. Per avviare il server di sviluppo con auto-reload:

```bash
uvicorn main:app --reload
```

Una volta avviato, apri il browser all'indirizzo:
👉 **[http://127.0.0.1:8000/dashboard](http://127.0.0.1:8000/dashboard)**

Dalla dashboard puoi:
* Visualizzare lo stato di recupero (Readiness Score) e i grafici dell'HRV degli ultimi 10 giorni.
* Monitorare gli allenamenti recenti e il prossimo allenamento pianificato.
* Caricare un file `.gpx` per la segmentazione avanzata (con parametri personalizzabili per riscaldamento, defaticamento, ripetute, ecc.).
* Visualizzare i target nutrizionali e i macronutrienti raccomandati.

### 4. Esecuzione dei Test di Architettura
Per validare l'integrità del database, le funzioni di smoothing del parser GPX e il funzionamento del pipeline multi-agente LangGraph, esegui:

```bash
python test_arch.py
```
