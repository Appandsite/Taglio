# Taglio

Piattaforma per aiutare aziende — anche piccole, con budget limitati — a
trovare il posizionamento pubblicitario giusto su giornali e riviste
italiane: propone un piano tarato sul budget indicato (non solo le testate
più costose), stima il prezzo, suggerisce per ogni testata online una zona
di pagina meno affollata da altri inserzionisti, indica un contatto reale
per avviare la trattativa (link trovato sul sito stesso della testata, mai
inventato), e genera consigli strategici (punti di forza, spazi lasciati
liberi dai competitor) e idee creative — una sicura e alcune più audaci —
in tempo reale. Il campo "competitor" nel wizard alimenta il generatore di
idee/strategia (per calibrare tono e riferimenti), non l'allocazione del
budget: lo scraper rileva reti pubblicitarie attive per testata, non le
creatività dei singoli inserzionisti, quindi non può identificare dove
specificamente un competitor con nome è posizionato — vedi limiti sotto.

## Architettura

```
taglio-project/
├── render.yaml                → blueprint di deploy per Render (deve stare
│                                 in root perché Render lo trovi da solo;
│                                 "rootDir: scraper" al suo interno gli dice
│                                 poi dove girano davvero i comandi)
├── site/
│   └── taglio-demo.html      → frontend: wizard onboarding + dashboard risultati
│                                (chiama l'API di Claude per le idee creative,
│                                 e il backend locale per l'allocazione testate)
└── scraper/
    ├── scraper.py             → visita le testate e rileva gli ad slot attivi
    ├── taxonomy.py            → mappa settore azienda -> categorie di testate
    ├── config.yaml            → elenco testate (62), ciascuna con categorie
    ├── aggregator.py          → riassume gli scan grezzi per testata
    ├── api.py                 → backend FastAPI che serve i dati al sito
    ├── requirements.txt       → dipendenze scraper + API (uso locale)
    ├── requirements-api.txt   → solo dipendenze API, per il deploy online
    └── Procfile               → comando di avvio, alternativa a render.yaml
                                  (es. per Railway, che non legge render.yaml)
```

**Flusso dati**: `scraper.py` produce JSON grezzi → `aggregator.py` li
riassume in `aggregated.json`, allegando le categorie di ogni testata da
`config.yaml` → `api.py` li serve via `/api/allocation?settore=...` →
`site/taglio-demo.html` li mostra nella dashboard, con fallback automatico
a dati simulati se il backend non è raggiungibile.

Le idee creative sono generate in tempo reale chiamando l'API di Claude
direttamente dal frontend (funziona quando il sito gira come artifact
dentro Claude.ai; fuori da lì, o se la chiamata fallisce, scatta il
fallback a idee di esempio).

## Avviare tutto in locale

```bash
cd scraper
python -m venv venv && source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

# 1. Scansiona le testate (filtrabile per settore)
python scraper.py --config config.yaml --output out/ --settore automotive

# 2. Aggrega i risultati
python aggregator.py --input out/ --output aggregated.json --config config.yaml

# 3. Avvia il backend
uvicorn api:app --reload --port 8000
```

Poi apri `site/taglio-demo.html` in Claude.ai come artifact (per il motore
idee AI) o direttamente nel browser (userai solo i dati di allocazione dal
backend locale, con fallback mock per le idee).

## Stato del progetto — cosa è reale e cosa è ancora simulato

| Pezzo | Stato |
|---|---|
| Wizard onboarding, UI, dashboard | Funzionante |
| Generazione idee creative | Reale (via API Claude), con fallback mock |
| Scraping ad slot | Reale, testato dall'utente su 62 testate |
| Filtro testate per settore | Reale, collegato scraper → aggregator → API |
| Allocazione budget per testata | Reale se il backend è attivo e ha dati, altrimenti mock |
| Stima prezzi | Modello indicativo da benchmark pubblici, non tariffe ufficiali |
| Mockup grafico idee | Generato client-side (SVG), layout di riferimento non asset finale |
| Backend online (non-localhost) | Codice su GitHub, deploy Render in corso — vedi sotto |
| Gestione cookie banner nello scraper | Fatto |
| Storico nel tempo (durata campagne) | Fatto |
| Piano tarato sul budget (non sempre i grandi nazionali) | Fatto |
| Suggerimento posizionamento per testata online | Fatto, ma è densità di inserzionisti per zona pagina, non identificazione di competitor specifici — vedi nota legale/limiti sotto |
| Contatto pubblicitario per testata (link, non telefono/mail indovinati) | Fatto se lo scraper trova un link "Pubblicità" sul sito; altrimenti link diretto al sito reale della testata |
| Punti di forza e spazi bianchi rispetto ai competitor | Reale (via API Claude, stesse ipotesi di lavoro delle idee creative), con fallback mock per settore |

## Prossimi passi naturali, in ordine di impatto

1. ~~**Gestione cookie banner nello scraper**~~ — fatto: `scraper.py` ora
   riconosce i CMP più diffusi (OneTrust, Iubenda, Cookiebot, Didomi,
   Quantcast/TCF) via `dismiss_cookie_banner()`, con fallback su ricerca
   testuale dei pulsanti di accettazione ("Accetta tutto", "Accept all", ecc.)
   anche dentro iframe di terze parti.
2. ~~**Storico nel tempo**~~ — fatto: `aggregator.py` usa i timestamp di ogni
   osservazione per calcolare, per ciascuna testata, da quanti giorni
   consecutivi (fino all'ultima scansione) un dato dominio ad resta presente
   — campo `campagna_probabile` in `aggregated.json`, mostrato nel sito come
   badge "Campagna in corso da N giorni" quando lo streak è di almeno 2
   giorni. Il segnale migliora accumulando più scansioni via cron.
3. ~~**Deploy del backend online**~~ — in corso: `render.yaml` (in root,
   con `rootDir: scraper`), `Procfile` e `requirements-api.txt` sono pronti
   per un deploy su [Render](https://render.com) (piano free). Il codice è
   già su GitHub. Passi che restano da fare tu (serve un account sul
   servizio, non automatizzabile da qui):
   - Su Render: **New → Blueprint**, collega il repo `Taglio`. Render trova
     `render.yaml` in root automaticamente e propone il servizio
     `taglio-api` — conferma e avvia il deploy.
   - `aggregated.json` deve restare committato nel repo (il servizio serve
     solo dati già generati, non fa scraping da solo — lo scraping resta un
     processo locale/cron, vedi sotto). Dopo ogni scan+aggregate locale,
     `git add scraper/aggregated.json && git commit && git push`: se
     l'auto-deploy di Render è attivo (lo è di default sul branch
     collegato), il nuovo deploy parte da solo al push.
   - Aggiorna `CORS` in `api.py` (`allow_origins`) e `API_BASE_URL` in
     `site/taglio-demo.html` con l'URL pubblico assegnato da Render (es.
     `https://taglio-api.onrender.com`), poi ripubblica il sito.
4. **Database persistente**: sostituire i file JSON con Postgres quando il
   volume di dati/testate cresce.
5. ~~**Piano tarato sul budget + posizionamento per testata**~~ — fatto:
   prima il piano mostrava sempre le stesse 3-4 testate principali (le più
   "importanti"), a prescindere dal budget indicato — una piccola azienda si
   vedeva proposto Corriere della Sera anche con un budget che non lo
   copriva nemmeno per un'uscita. `site/taglio-demo.html` ora ha
   `buildAllocationPlan()`: stima il costo di ogni testata (`estimateCost`,
   con fasce di prezzo per ~60 testate reali invece delle poche di prima),
   scarta quelle fuori portata, e bilancia rilevanza e sostenibilità
   economica per preferire un mix diversificato — quotidiani locali,
   settimanali, verticali digitali — invece di poche testate costose. I dati
   mock per settore sono stati diversificati allo stesso modo. Inoltre,
   `aggregator.py` calcola per ogni testata online una zona di pagina
   (sopra/sotto la piega) con meno reti pubblicitarie già rilevate, mostrata
   nel sito come suggerimento di posizionamento.
6. ~~**Contatto pubblicitario + consigli strategici**~~ — fatto:
   `scraper.py` cerca ora, tra i link della pagina stessa (di solito nel
   footer), quello alla sezione commerciale/pubblicitaria — mai un
   numero/mail indovinati, solo un link che la testata pubblica da sola.
   Verificato dal vivo: su La Repubblica ha trovato
   `manzoniadvertising.com`, la concessionaria reale del gruppo GEDI. Se lo
   scraper non trova nulla (o per i dati mock), il sito mostra comunque un
   link diretto al sito reale della testata ("Vai al sito e cerca
   'Pubblicità'"), mai un contatto fittizio. In parallelo, il prompt AI che
   genera le idee creative ora produce anche `punti_di_forza` e
   `spazi_bianchi` — 2-3 ipotesi di lavoro ciascuno su cosa valorizzare
   dell'azienda e quali angoli/canali i competitor indicati sembrano
   presidiare meno, esplicitamente presentate come ipotesi da validare, non
   dati misurati (a differenza dei segnali sopra, questi vengono dal
   ragionamento del modello, non dallo scraping).

## Limiti da conoscere: identificazione dei competitor

Lo scraper (per scelta dichiarata nella sua docstring) non legge il
contenuto o la creatività degli annunci, solo quali domini di ad server
sono attivi su una testata. Questo significa che **non può dire "il
competitor X è posizionato qui"** — può solo dire "questa zona di pagina ha
N reti pubblicitarie attive, quest'altra ne ha meno". Il suggerimento di
posizionamento nel sito (`posizionamento_consigliato`) si basa su questo
secondo segnale, più debole ma verificabile, non sul primo. Se in futuro
serve un'identificazione più specifica dei competitor, servirebbe un
approccio diverso (es. verificare se il sito del competitor stesso cita
dove pubblicizza, o un'analisi manuale) — non è nello scope attuale dello
scraper.

## Nota legale, da non perdere di vista scalando il progetto

Lo scraper controlla `robots.txt` automaticamente, ma non è l'unico vincolo:
molti editori vietano lo scraping automatizzato nei propri termini di
servizio anche quando `robots.txt` non lo blocca tecnicamente. Per un uso
oltre il test personale, vale la pena valutare accordi diretti con le
concessionarie pubblicitarie (Manzoni, RCS, ecc.) invece di affidarsi solo
allo scraping — discusso più a fondo nelle conversazioni precedenti su
questo progetto.
