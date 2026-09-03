# Taglio

Piattaforma per aiutare aziende — anche piccole, con budget limitati — a
trovare il posizionamento pubblicitario giusto su giornali e riviste
italiane: propone un piano tarato sul budget indicato (non solo le testate
più costose), stima il prezzo, suggerisce per ogni testata online una zona
di pagina meno affollata da altri inserzionisti, indica un contatto reale
per avviare la trattativa (link trovato sul sito stesso della testata, mai
inventato), e genera consigli su misura concreti e idee creative — una
sicura e alcune più audaci —
in tempo reale, personalizzate su prodotto/servizio specifico, zona
geografica e target di clientela indicati nel wizard — e, quando azienda o
competitor indicano un sito web, lette davvero (il backend scarica la
pagina, l'AI ragiona sul contenuto reale, non solo sul nome/URL). La zona
geografica indicata dà anche una spinta alle testate locali pertinenti nel
piano di allocazione (es. "Bergamo" → L'Eco di Bergamo pesa di più). Il
campo "competitor" alimenta il generatore di idee/strategia, non
l'allocazione del budget: lo scraper rileva reti pubblicitarie attive per
testata, non le creatività dei singoli inserzionisti, quindi non può
identificare dove specificamente un competitor con nome è posizionato —
vedi limiti sotto.

Accesso: richiede login (email + password, via Supabase Auth). La prima
ricerca è gratuita per utente, poi serve un abbonamento di 4,99€/mese
(Stripe) — vedi "Accesso e abbonamento" più sotto per la configurazione.

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
│                                 e il backend Render per l'allocazione testate)
├── docs/
│   └── index.html            → COPIA di site/taglio-demo.html, serve solo a
│                                GitHub Pages (che pubblica da /docs o dalla
│                                root, non da sottocartelle a piacere). Dopo
│                                ogni modifica a site/taglio-demo.html vanno
│                                copiati anche qui, altrimenti la versione
│                                pubblica su GitHub Pages resta indietro.
└── scraper/
    ├── scraper.py             → visita le testate e rileva gli ad slot attivi
    ├── taxonomy.py            → mappa settore azienda -> categorie di testate
    ├── config.yaml            → elenco testate (67), ciascuna con categorie
    ├── aggregator.py          → riassume gli scan grezzi per testata
    ├── api.py                 → backend FastAPI: dati al sito, checkout e
    │                            webhook Stripe, lettura reale di siti
    │                            azienda/competitor per l'AI
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
| Allocazione budget per testata | Reale — backend sempre online; fallback mock solo se Render è giù |
| Stima prezzi | Modello indicativo da benchmark pubblici, non tariffe ufficiali |
| Mockup grafico idee | Generato client-side (SVG), layout di riferimento non asset finale |
| Backend online (non-localhost) | Fatto — https://taglio-api.onrender.com, dati reali (67 testate tracciate) |
| Gestione cookie banner nello scraper | Fatto |
| Storico nel tempo (durata campagne) | Fatto |
| Piano tarato sul budget (non sempre i grandi nazionali) | Fatto |
| Suggerimento posizionamento per testata online | Fatto, ma è densità di inserzionisti per zona pagina, non identificazione di competitor specifici — vedi nota legale/limiti sotto |
| Login/registrazione utenti | Fatto (Supabase Auth) |
| Limite 1 ricerca gratuita + conteggio per utente | Fatto (Supabase) |
| Abbonamento 4,99€/mese | Fatto — Stripe collegato in modalità live, pagamenti reali |
| Lettura reale del sito azienda/competitor per l'AI | Fatto (`/api/fetch-site-summary`: protezione SSRF, rileva siti generati via JavaScript invece di leggerli come vuoti, segue un eventuale link "chi siamo") |
| Campi prodotto/zona/target nel wizard | Fatto, usati nel prompt AI |
| Boost testate locali in base alla zona indicata | Fatto (mappa regioni → testate in `site/taglio-demo.html`) |
| Copertura settimanali nello scraper | Ampliata: +TV Sorrisi e Canzoni, Vero, Diva e Donna, Confidenze, Autosprint |
| Contatto pubblicitario per testata (link, non telefono/mail indovinati) | Fatto se lo scraper trova un link "Pubblicità" sul sito; altrimenti link diretto al sito reale della testata |
| Consigli su misura + analisi azienda | Reale (via API Claude), basati sul contenuto letto dal sito quando disponibile — fallback mock per settore altrimenti |

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
3. ~~**Deploy del backend online**~~ — fatto: il codice è su GitHub
   (`github.com/Appandsite/Taglio`), il backend gira su Render come
   servizio `taglio-api` (piano free) su **https://taglio-api.onrender.com**,
   e `site/taglio-demo.html` (`API_BASE_URL`) punta lì invece che a
   localhost. `render.yaml` sta nella root del repo con `rootDir: scraper`:
   Render cerca il blueprint solo in root, non nelle sottocartelle, quindi
   il file va tenuto lì anche in futuro.
   - **Mantenere aggiornati i dati**: rifai scan + aggregate in locale
     (vedi sopra), poi `git add scraper/aggregated.json && git commit &&
     git push` — l'auto-deploy di Render riparte da solo al push.
   - **Piano free e "sonno"**: dopo un periodo di inattività Render mette in
     pausa il servizio; la prima richiesta successiva impiega 30-50 secondi
     a risvegliarlo (normale, non un bug). Se diventa un problema, il piano
     a pagamento di Render lo evita, oppure si può pingare l'endpoint
     `/api/health` periodicamente per tenerlo sveglio.
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
   genera le idee creative produce anche `analisi_azienda` e
   `consigli_su_misura` — 4-5 consigli operativi, non punti di forza
   astratti, ognuno agganciato a un dato specifico (prodotto, zona, target,
   o contenuto realmente letto dal sito azienda/competitor). Prima erano
   "punti di forza" e "spazi bianchi" generici, sostituiti dopo che i primi
   test hanno mostrato consigli troppo slegati dal cliente reale — vedi
   "Ricerche cucite sul cliente" più sotto per il dettaglio.
7. **Sito pubblico senza account Claude**: l'artifact su claude.ai richiede
   un account per essere aperto. Per un link davvero pubblico, `docs/`
   contiene una copia del sito pensata per **GitHub Pages**:
   - Su GitHub: repo → **Settings → Pages** → Source: **Deploy from a
     branch** → Branch: **main**, cartella **/docs** → **Save**.
   - Se l'opzione Pages non compare (capita sui repo privati dei piani
     gratuiti più vecchi), rendi il repo pubblico da **Settings → Danger
     Zone → Change visibility** — il progetto non contiene chiavi o dati
     sensibili, è sicuro farlo.
   - URL risultante: `https://<utente>.github.io/<repo>/`.
   - **Compromesso**: su GitHub Pages la generazione idee via AI (che
     chiama l'API di Claude direttamente dal browser) non funziona — è
     una capacità disponibile solo dentro l'ambiente artifact di
     Claude.ai — quindi scatta sempre il fallback con le idee di esempio.
     Budget, testate e contatti (dal backend Render) restano invece
     pienamente reali e funzionanti.
   - Ricorda: `docs/index.html` è una copia di `site/taglio-demo.html`,
     va aggiornata a mano dopo ogni modifica al sito (vedi sopra).
8. **Accesso e abbonamento**: login obbligatorio, 1 ricerca gratuita per
   utente poi 4,99€/mese — vedi la sezione dedicata subito sotto per lo
   stato attuale e cosa manca.

## Accesso e abbonamento (Supabase + Stripe)

**Stato**: tutto attivo e funzionante — login/registrazione, conteggio
ricerche gratuite (Supabase), creazione pagamento e sblocco abbonamento via
webhook (Stripe, **modalità live**: i pagamenti sono reali). Se in futuro
le chiavi Stripe dovessero mancare o essere revocate su Render,
`/api/create-checkout-session` risponde con un errore gestito ("pagamenti
non ancora configurati") invece di rompersi.

### 1. Supabase (fatto)

Progetto: `oxuirmbgbwnegnqxfdjx` (regione EU, Londra). Tabella `profiles`,
creata con questo schema (uno-a-uno con `auth.users`, un trigger la popola
automaticamente alla registrazione, RLS abilitata così ogni utente vede
solo il proprio profilo):

```sql
create table public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  ricerche_usate int not null default 0,
  abbonato boolean not null default false,
  stripe_customer_id text,
  stripe_subscription_id text,
  created_at timestamptz not null default now()
);

create function public.handle_new_user()
returns trigger as $$
begin
  insert into public.profiles (id) values (new.id);
  return new;
end;
$$ language plpgsql security definer;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

alter table public.profiles enable row level security;
create policy "Utenti leggono il proprio profilo"
  on public.profiles for select using (auth.uid() = id);
create policy "Utenti aggiornano il proprio profilo"
  on public.profiles for update using (auth.uid() = id);
```

`SUPABASE_URL` e la chiave `anon` sono già nel codice di
`site/taglio-demo.html` (sono pensate per stare nel frontend, non sono
segrete). La chiave `service_role` invece **non va mai nel frontend**: la
userà solo `api.py` sul backend, letta dalla variabile d'ambiente
`SUPABASE_SERVICE_ROLE_KEY` su Render (Settings → Environment).

### 2. Stripe (fatto)

1. Crea un account su [stripe.com](https://stripe.com) (richiede dati
   bancari/fiscali reali per incassare, ma si può iniziare in modalità
   test).
2. **Product catalog → Add product**: nome "Abbonamento Taglio", prezzo
   ricorrente **4,99€ / mese**. Copia l'ID del prezzo (`price_...`).
3. **Developers → API keys**: copia la **Secret key** (`sk_...`).
4. **Developers → Webhooks → Add endpoint**: URL
   `https://taglio-api.onrender.com/api/stripe-webhook`, eventi da
   ascoltare: `checkout.session.completed`, `customer.subscription.updated`,
   `customer.subscription.deleted`. Copia il **Signing secret** (`whsec_...`).

### 3. Collegare tutto su Render (fatto)

Su Render → servizio `taglio-api` → **Environment**, aggiungi:

| Variabile | Valore |
|---|---|
| `SUPABASE_SERVICE_ROLE_KEY` | dalla dashboard Supabase → Project Settings → API |
| `STRIPE_SECRET_KEY` | dal passo 2.3 sopra |
| `STRIPE_PRICE_ID` | dal passo 2.2 sopra |
| `STRIPE_WEBHOOK_SECRET` | dal passo 2.4 sopra |
| `SITE_URL` | `https://appandsite.github.io/Taglio/` (o l'URL pubblico attuale) |

Render riavvia il servizio da solo dopo aver salvato le variabili — non
serve un nuovo deploy manuale. A quel punto il pulsante "Abbonati ora" nel
sito porta davvero a una pagina di pagamento Stripe, e al pagamento andato
a buon fine il webhook sblocca l'utente su Supabase.

**Consiglio**: testa tutto prima con le chiavi Stripe in **modalità test**
(carte di prova, nessun addebito reale) prima di passare alle chiavi live.

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
