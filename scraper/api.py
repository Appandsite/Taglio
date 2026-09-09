"""
Taglio — backend API
======================

Espone i dati aggregati dello scraper al sito via HTTP, così il frontend
può leggerli con una semplice fetch() invece di dati finti. Gestisce anche
l'abbonamento a pagamento (Stripe) e lo stato "abbonato" degli utenti
(Supabase): dopo le ricerche gratuite, il sito chiama /api/create-checkout-
session per far pagare l'utente, e Stripe notifica il pagamento riuscito a
/api/stripe-webhook, che aggiorna il profilo su Supabase. /api/fetch-site-
summary legge davvero il sito dell'azienda o di un competitor indicato nel
wizard. /api/generate-analysis chiama Anthropic da qui (mai dal browser) per
produrre analisi e consigli veri sul prodotto pubblico — vedi la sezione
dedicata più sotto.

Uso:
    uvicorn api:app --reload --port 8000

Poi apri http://localhost:8000/api/allocation nel browser per verificare
che risponda con il JSON prima di collegarlo al sito.

Variabili d'ambiente per l'abbonamento (da impostare su Render, non in
locale a meno di testare i pagamenti — vedi README):
    STRIPE_SECRET_KEY       chiave segreta Stripe (dashboard → Developers → API keys)
    STRIPE_PRICE_ID         ID del prezzo ricorrente creato su Stripe (price_...)
    STRIPE_WEBHOOK_SECRET   firma del webhook (dashboard → Developers → Webhooks)
    SUPABASE_URL            URL progetto Supabase
    SUPABASE_SERVICE_ROLE_KEY  chiave service_role Supabase (mai nel frontend!)
    SITE_URL                URL pubblico del sito, per il redirect dopo il pagamento
Finché STRIPE_SECRET_KEY manca, /api/create-checkout-session risponde con un
errore chiaro invece di rompersi — l'app resta usabile, solo l'abbonamento
non è ancora attivabile.

Variabile d'ambiente per l'AI (da impostare su Render):
    ANTHROPIC_API_KEY       chiave segreta Anthropic — SOLO qui, mai nel
                             frontend, mai committata, mai loggata. Finché
                             manca, /api/generate-analysis risponde 503 con
                             un messaggio chiaro invece di rompersi.
    ANTHROPIC_MODEL         opzionale, default "claude-sonnet-5"
"""

import ipaddress
import json
import logging
import os
import re
import socket
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
import stripe
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("taglio")


def _estrai_oggetto_json(text: str) -> Optional[str]:
    """Estrae il primo oggetto JSON bilanciato da un testo che può avere
    prosa prima e/o dopo (osservato con web_search attivo: il modello a
    volte aggiunge una frase introduttiva o un commento finale nonostante
    l'istruzione di rispondere SOLO con JSON). Un semplice regex "greedy"
    dal primo '{' all'ultimo '}' del testo intero si rompe non appena c'è
    QUALSIASI '{' o '}' dopo il JSON vero (anche dentro un blocco di
    codice di esempio nel commento finale) — qui invece si conta la
    profondità delle graffe rispettando le stringhe tra virgolette, così
    ci si ferma esattamente alla graffa di chiusura corrispondente alla
    prima di apertura."""
    inizio = text.find("{")
    if inizio == -1:
        return None
    profondita = 0
    dentro_stringa = False
    escape = False
    for i in range(inizio, len(text)):
        ch = text[i]
        if dentro_stringa:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                dentro_stringa = False
            continue
        if ch == '"':
            dentro_stringa = True
        elif ch == "{":
            profondita += 1
        elif ch == "}":
            profondita -= 1
            if profondita == 0:
                return text[inizio:i + 1]
    return None

from taxonomy import categorie_rilevanti

# ---------------------------------------------------------------------------
# Geo-classificazione testate del catalogo (Lombardia ecc.) — stessa tabella
# già validata lato frontend (site/taglio-demo.html, GEO_TESTATA) l'8-9/9/2026
# per il fix del ranking geografico. Portata qui in Python perché ora anche
# il backend deve calcolare geo_fit per le testate di catalogo, in modo
# uniforme con i media scoperti via web search (istruzioni del 9/9/2026,
# "Taglio 3.0").
# ---------------------------------------------------------------------------
GEO_TESTATA = [
    {"regione": "Lombardia", "keywords": ["milano", "lombardia", "bergamo", "brescia", "monza", "como", "pavia", "cremona", "mantova", "varese", "lecco", "lodi", "sondrio"],
     "testate": [("il giorno", "REGION"), ("eco di bergamo", "LOCAL")]},
    {"regione": "Friuli-Venezia Giulia", "keywords": ["trieste", "friuli", "venezia giulia", "fvg", "udine", "pordenone", "gorizia"],
     "testate": [("il piccolo", "REGION")]},
    {"regione": "Sardegna", "keywords": ["sardegna", "cagliari", "sassari", "nuoro", "oristano"],
     "testate": [("nuova sardegna", "REGION"), ("unione sarda", "REGION")]},
    {"regione": "Sicilia", "keywords": ["sicilia", "palermo", "catania", "messina", "siracusa", "trapani", "ragusa", "agrigento", "caltanissetta", "enna"],
     "testate": [("giornale di sicilia", "REGION"), ("quotidiano di sicilia", "REGION")]},
    {"regione": "Calabria", "keywords": ["calabria", "reggio calabria", "catanzaro", "cosenza", "crotone", "vibo valentia"],
     "testate": [("gazzetta del sud", "REGION")]},
    {"regione": "Puglia", "keywords": ["puglia", "bari", "foggia", "lecce", "taranto", "brindisi", "barletta"],
     "testate": [("gazzetta del mezzogiorno", "REGION"), ("quotidiano di puglia", "REGION")]},
    {"regione": "Basilicata", "keywords": ["basilicata", "potenza", "matera"],
     "testate": [("quotidiano del sud", "REGION")]},
    {"regione": "Toscana", "keywords": ["toscana", "firenze", "livorno", "pisa", "siena", "arezzo", "prato", "lucca", "grosseto"],
     "testate": [("la nazione", "REGION"), ("tirreno", "REGION")]},
    {"regione": "Emilia-Romagna", "keywords": ["emilia", "bologna", "romagna", "modena", "parma", "reggio emilia", "ferrara", "ravenna", "rimini", "piacenza"],
     "testate": [("resto del carlino", "REGION")]},
    {"regione": "Campania", "keywords": ["campania", "napoli", "salerno", "caserta", "avellino", "benevento"],
     "testate": [("il mattino", "REGION")]},
    {"regione": "Marche", "keywords": ["marche", "ancona", "pesaro", "macerata", "ascoli"],
     "testate": [("corriere adriatico", "REGION")]},
    {"regione": "Liguria", "keywords": ["liguria", "genova", "la spezia", "savona", "imperia"],
     "testate": [("secolo xix", "REGION")]},
    {"regione": "Lazio", "keywords": ["lazio", "roma", "latina", "frosinone", "viterbo", "rieti"],
     "testate": [("il tempo", "REGION"), ("il messaggero", "REGION")]},
]


def _classifica_geo_testata_catalogo(nome: str) -> tuple[str, Optional[str]]:
    """(scope, regione) per una testata del catalogo config.yaml. NATIONAL se
    non è in nessun gruppo sopra (i grandi quotidiani/settimanali nazionali e
    i verticali online restano sempre candidati, mai penalizzati per zona)."""
    n = nome.lower()
    for gruppo in GEO_TESTATA:
        for match, scope in gruppo["testate"]:
            if match in n:
                return scope, gruppo["regione"]
    return "NATIONAL", None


def _regione_da_testo_catalogo(testo: Optional[str]) -> Optional[str]:
    if not testo:
        return None
    t = testo.lower()
    for gruppo in GEO_TESTATA:
        if any(k in t for k in gruppo["keywords"]):
            return gruppo["regione"]
    return None


# ---------------------------------------------------------------------------
# Stima prezzi per le testate di CATALOGO (stessa tabella già in uso lato
# frontend, site/taglio-demo.html — FORMATO_BASE/FASCIA_TESTATA). Portata qui
# perché ora il TAGLIO_ESTIMATE va calcolato lato server per stare nello
# stesso oggetto "media" dei risultati di discovery (istruzioni 9/9/2026,
# "Taglio 3.0", gerarchia prezzi). Solo per testate di catalogo: un media
# appena scoperto non ha una fascia nota, resta PRICE_ON_REQUEST/UNKNOWN.
# ---------------------------------------------------------------------------
FORMATO_BASE = {
    "doppia pagina": (32000, 70000, "a uscita"),
    "pagina intera": (18000, 42000, "a uscita"),
    "mezza pagina": (9000, 22000, "a uscita"),
    "banner": (900, 3200, "a settimana"),
    "native": (3000, 9000, "a uscita/settimana"),
    "default": (5000, 14000, "a uscita"),
}
PERIODO_USCITE = {"2settimane": 2, "1mese": 4, "3mesi": 12, "stagionale": 8}
FASCIA_TESTATA = [
    (["corriere della sera"], 1.2), (["repubblica"], 1.1), (["sole 24"], 1.15),
    (["stampa"], 1.05), (["messaggero"], 0.9), (["fatto quotidiano"], 0.8),
    (["il giornale"], 0.75), (["libero quotidiano"], 0.75), (["avvenire"], 0.7),
    (["manifesto"], 0.6), (["domani"], 0.65), (["verità"], 0.65), (["il tempo"], 0.85),
    (["resto del carlino", "la nazione", "il giorno", "secolo xix", "mattino",
      "giornale di sicilia", "gazzetta del sud", "gazzetta del mezzogiorno",
      "quotidiano del sud", "quotidiano di sicilia", "tirreno", "piccolo",
      "nuova sardegna", "unione sarda", "eco di bergamo", "corriere adriatico",
      "quotidiano di puglia"], 0.4),
    (["milano finanza", "mf "], 0.85), (["italia oggi"], 0.6),
    (["gazzetta dello sport"], 0.9), (["corriere dello sport", "tuttosport"], 0.75),
    (["panorama", "espresso"], 0.55), (["internazionale", "focus"], 0.5),
    (["famiglia cristiana"], 0.45), (["sorrisi"], 0.5), (["chi"], 0.5),
    (["gente", "oggi"], 0.45), (["novella 2000"], 0.4), (["dipiù"], 0.35),
    (["vero"], 0.4), (["diva e donna"], 0.4), (["vogue"], 1.3),
    (["vanity fair"], 0.7), (["elle"], 0.7), (["grazia", "io donna", "gq italia"], 0.6),
    (["donna moderna", "amica"], 0.55), (["confidenze"], 0.45), (["wired"], 0.6),
    (["hdblog", "dday", "tom's hardware", "hardware upgrade"], 0.4),
    (["punto informatico"], 0.35), (["quattroruote", "gambero rosso"], 0.6),
    (["autosprint"], 0.55), (["cucina italiana"], 0.8), (["dissapore"], 0.4),
]


def _fascia_da_nome(nome: str) -> float:
    n = nome.lower()
    for keywords, fascia in FASCIA_TESTATA:
        if any(k in n for k in keywords):
            return fascia
    return 0.5


def _formato_key_da_stringa(formato: Optional[str]) -> str:
    f = (formato or "").lower()
    if "doppia pagina" in f:
        return "doppia pagina"
    if "pagina intera" in f:
        return "pagina intera"
    if "mezza pagina" in f:
        return "mezza pagina"
    if "banner" in f or "leaderboard" in f:
        return "banner"
    if "native" in f:
        return "native"
    return "default"


def _stima_prezzo_catalogo(nome: str, formato: Optional[str], formato_categoria: Optional[str], periodo_key: str) -> dict:
    """TAGLIO_ESTIMATE per una testata di catalogo con dati reali osservati.
    Mai per un media appena scoperto (nessuna fascia nota per quello)."""
    key = formato_categoria if formato_categoria in FORMATO_BASE else _formato_key_da_stringa(formato)
    base_min, base_max, unit = FORMATO_BASE.get(key, FORMATO_BASE["default"])
    fascia = _fascia_da_nome(nome)
    uscite = PERIODO_USCITE.get(periodo_key, 4)
    per_uscita_min = round(base_min * fascia / 500) * 500
    per_uscita_max = round(base_max * fascia / 500) * 500
    return {
        "tier": "TAGLIO_ESTIMATE",
        "min": per_uscita_min * uscite,
        "max": per_uscita_max * uscite,
        "unit": unit,
        "fonte": "Stima Taglio — non è un listino ufficiale della testata.",
    }

app = FastAPI(title="Taglio API")

# In sviluppo va bene aperto a tutti ("*"). In produzione limita
# allow_origins al dominio reale dove pubblichi il sito.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

AGGREGATED_FILE = Path("aggregated.json")
DATASET_META_FILE = Path("dataset_meta.json")
CONFIG_FILE = Path("config.yaml")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://oxuirmbgbwnegnqxfdjx.supabase.co")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
SITE_URL = os.environ.get("SITE_URL", "https://appandsite.github.io/Taglio/")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def _load_testate_config() -> dict:
    """{nome_testata: [categorie]} da config.yaml. Dizionario vuoto se il
    file manca, così l'endpoint degrada invece di rompersi."""
    if not CONFIG_FILE.exists():
        return {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    return {t["name"]: t.get("categorie", ["generalista"]) for t in config.get("testate", [])}


def _load_testate_urls() -> dict:
    """{nome_testata: dominio_canonico} — serve a far combaciare un media
    scoperto dall'AI con una testata già nel catalogo (istruzioni 9/9/2026,
    "Taglio 3.0": le 67 testate sono parte del catalogo, non un universo a
    parte)."""
    if not CONFIG_FILE.exists():
        return {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    return {t["name"]: _canonical_domain(t["url"]) for t in config.get("testate", []) if t.get("url")}


@app.get("/api/allocation")
def get_allocation(settore: Optional[str] = None):
    """Ritorna TUTTE le testate configurate rilevanti per il settore, non
    solo quelle con dati raccolti: una testata monitorata ma senza
    rilevazioni compare comunque, con stato "dati_non_disponibili" e ogni
    campo osservato a null — mai un formato/posizionamento/contatto
    inventato per riempire il vuoto (vedi audit del 9/9)."""
    testate_config = _load_testate_config()
    if not testate_config:
        raise HTTPException(status_code=500, detail="config.yaml non trovato o vuoto sul server.")

    aggregated_by_nome = {}
    if AGGREGATED_FILE.exists():
        with open(AGGREGATED_FILE, "r", encoding="utf-8") as f:
            for row in json.load(f):
                aggregated_by_nome[row["nome"]] = row

    allowed = categorie_rilevanti(settore) if settore else None

    risultati = []
    for nome, categorie in testate_config.items():
        if allowed and not (set(categorie) & allowed):
            continue
        if nome in aggregated_by_nome:
            row = dict(aggregated_by_nome[nome])
            row["stato"] = "osservato"
        else:
            row = {
                "nome": nome,
                "categorie": categorie,
                "stato": "dati_non_disponibili",
                "formato": None,
                "formato_categoria": None,
                "quota": None,
                "segnali_osservati": 0,
                "domini_ad_distinti": [],
                "prima_osservazione": None,
                "ultima_osservazione": None,
                "giorni_scansionati": 0,
                "campagna_probabile": None,
                "posizionamento_consigliato": None,
                "contatto_pubblicitario_url": None,
            }
        risultati.append(row)

    return risultati


@app.get("/api/dataset-status")
def dataset_status():
    """Numeri reali sul dataset corrente, per mostrare in UI frasi come
    "67 testate monitorate, 40 con rilevazioni, ultimo aggiornamento ..."
    senza mai hardcodare un conteggio nel frontend (vedi audit del 9/9,
    punto 9 — la vecchia frase "62 testate" non veniva mai ricalcolata)."""
    testate_config = _load_testate_config()
    testate_configurate = len(testate_config)

    testate_con_dati = 0
    ultimo_aggiornamento = None
    if AGGREGATED_FILE.exists():
        with open(AGGREGATED_FILE, "r", encoding="utf-8") as f:
            aggregated = json.load(f)
        testate_con_dati = len(aggregated)
        date_osservate = [r.get("ultima_osservazione") for r in aggregated if r.get("ultima_osservazione")]
        if date_osservate:
            ultimo_aggiornamento = max(date_osservate)

    meta = {}
    if DATASET_META_FILE.exists():
        with open(DATASET_META_FILE, "r", encoding="utf-8") as f:
            meta = json.load(f)
        # Il file di metadati (prodotto dalla scansione automatica, vedi
        # scraper/validate_dataset.py) è la fonte più precisa per la data,
        # se disponibile: copre anche il caso di uno scan con 0 testate
        # valide, che altrimenti lascerebbe ultimo_aggiornamento a None.
        ultimo_aggiornamento = meta.get("ultimo_aggiornamento", ultimo_aggiornamento)

    return {
        "testate_configurate": testate_configurate,
        "testate_con_dati": testate_con_dati,
        "ultimo_aggiornamento": ultimo_aggiornamento,
        "ultima_scansione_automatica": meta or None,
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}


class CheckoutRequest(BaseModel):
    user_id: str
    email: str


@app.post("/api/create-checkout-session")
def create_checkout_session(payload: CheckoutRequest):
    if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
        raise HTTPException(
            status_code=503,
            detail="Pagamenti non ancora configurati (manca la chiave Stripe sul server).",
        )
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            customer_email=payload.email,
            # Collega la sessione di pagamento all'utente Supabase: il
            # webhook lo userà per sapere quale profilo sbloccare.
            client_reference_id=payload.user_id,
            success_url=SITE_URL + "?abbonamento=ok",
            cancel_url=SITE_URL + "?abbonamento=annullato",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"url": session.url}


def _supabase_headers() -> dict:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


def supabase_update_profile_by_id(user_id: str, **fields) -> None:
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/profiles?id=eq.{user_id}",
        headers={**_supabase_headers(), "Prefer": "return=minimal"},
        json=fields,
        timeout=10,
    )


def supabase_update_profile_by_subscription(subscription_id: str, **fields) -> None:
    """I webhook di aggiornamento/cancellazione abbonamento non portano
    l'id utente Supabase (solo l'id abbonamento Stripe, salvato in
    precedenza da checkout.session.completed), quindi cerchiamo il profilo
    a partire da quello."""
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/profiles?stripe_subscription_id=eq.{subscription_id}&select=id",
        headers=_supabase_headers(),
        timeout=10,
    )
    rows = resp.json() if resp.ok else []
    if rows:
        supabase_update_profile_by_id(rows[0]["id"], **fields)


@app.post("/api/stripe-webhook")
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=503, detail="Webhook non ancora configurato.")

    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    except Exception:
        # Firma non valida: non fidarsi del corpo della richiesta.
        raise HTTPException(status_code=400, detail="Firma del webhook non valida.")

    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type == "checkout.session.completed":
        user_id = obj.get("client_reference_id")
        if user_id:
            supabase_update_profile_by_id(
                user_id,
                abbonato=True,
                stripe_customer_id=obj.get("customer"),
                stripe_subscription_id=obj.get("subscription"),
            )
    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        supabase_update_profile_by_subscription(
            obj["id"],
            abbonato=(obj.get("status") == "active"),
        )

    return {"received": True}


def _is_safe_url(url: str) -> bool:
    """L'endpoint qui sotto scarica un URL scelto da chi usa il sito
    (l'azienda o un suo competitor): senza controlli sarebbe un classico
    varco SSRF verso la rete interna del server (IP privati, localhost,
    indirizzi cloud riservati). Accetta solo http/https con un hostname che
    risolve a un indirizzo pubblico."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        for info in socket.getaddrinfo(parsed.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


def _strip_html_text(html: str, max_chars: int = 3000) -> tuple[str, str]:
    """Estrae titolo e testo leggibile da una pagina HTML senza dipendenze
    pesanti (niente parser HTML completo): basta a dare all'AI un'idea
    reale di cosa fa il sito, non serve un'estrazione perfetta."""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""

    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z0-9#]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return title, text[:max_chars]


def _fetch_raw_html(url: str, timeout: int = 8) -> Optional[str]:
    resp = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 (compatible; TaglioBot/0.1)"},
        allow_redirects=False,
        stream=True,
    )
    if resp.status_code != 200:
        return None
    raw = resp.raw.read(300_000, decode_content=True)
    # requests, quando il Content-Type non dichiara un charset esplicito,
    # ripiega su ISO-8859-1 per lo standard HTTP — ma nella pratica la
    # stragrande maggioranza dei siti oggi è UTF-8 senza dichiararlo (i
    # browser fanno lo stesso ripiego). Usiamo l'encoding solo se dichiarato
    # esplicitamente nell'header, altrimenti UTF-8.
    content_type = resp.headers.get("content-type", "")
    charset_match = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    encoding = charset_match.group(1) if charset_match else "utf-8"
    try:
        return raw.decode(encoding, errors="ignore")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="ignore")


# Testi/href comuni per la pagina "chi siamo": la home spesso è tutta
# slogan ed hero visivo, i dettagli concreti su cosa fa davvero l'azienda
# stanno più spesso in una pagina interna come questa.
ABOUT_LINK_KEYWORDS = [
    "chi siamo", "chi-siamo", "chisiamo", "l'azienda", "l azienda", "azienda",
    "about", "about-us", "cosa facciamo", "la nostra storia", "company",
]


def _find_about_link(html: str, base_url: str) -> Optional[str]:
    """Cerca nella homepage un link a una pagina 'chi siamo'/'about' sullo
    stesso dominio (mai un altro sito: stesso principio prudenziale
    dell'SSRF-check, qui per restare nello scope della pagina richiesta)."""
    parsed_base = urlparse(base_url)
    for match in re.finditer(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.IGNORECASE | re.DOTALL):
        href = match.group(1)
        link_text = re.sub(r"<[^>]+>", " ", match.group(2)).strip().lower()
        href_lower = href.lower()
        if any(kw in link_text or kw in href_lower for kw in ABOUT_LINK_KEYWORDS):
            full_url = urljoin(base_url, href)
            parsed_link = urlparse(full_url)
            if parsed_link.scheme in ("http", "https") and parsed_link.netloc == parsed_base.netloc:
                return full_url
    return None


# ---------------------------------------------------------------------------
# Media discovery — catalogo dinamico e verifica pubblicitaria.
#
# Le 67 testate di config.yaml restano un catalogo PARTE del sistema, non
# l'universo chiuso entro cui scegliere (istruzioni 9/9/2026, "Taglio 3.0").
# Un media scoperto via AI (web search) viene verificato con una fetch reale
# (stessa infrastruttura SSRF-safe di /api/fetch-site-summary) alla ricerca
# di una pagina pubblicità/media kit — mai dato per "vendibile" solo perché
# l'AI lo ha nominato. I risultati vengono salvati in un catalogo su file
# (stesso pattern di aggregated.json) così una prossima ricerca non deve
# riverificare un dominio già controllato di recente (istruzioni, "cache").
# ---------------------------------------------------------------------------
MEDIA_CATALOG_FILE = Path("media_catalog.json")
CATALOG_CACHE_DAYS = 30
MAX_MEDIA_DA_VERIFICARE = 10  # tetto di fetch reali per ricerca — controllo costi

ADV_LINK_KEYWORDS = [
    "pubblicità", "pubblicita", "advertising", "media kit", "mediakit",
    "concessionaria", "inserzionisti", "info commerciali", "spazi pubblicitari",
]


def _find_advertising_link(html: str, base_url: str) -> Optional[str]:
    """Stesso principio di _find_about_link, ma cerca un link alla pagina
    pubblicitaria/media kit — l'evidenza concreta richiesta prima di
    considerare un media come "vendibile" (istruzioni 9/9/2026, punto 7)."""
    parsed_base = urlparse(base_url)
    for match in re.finditer(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.IGNORECASE | re.DOTALL):
        href = match.group(1)
        link_text = re.sub(r"<[^>]+>", " ", match.group(2)).strip().lower()
        href_lower = href.lower()
        if any(kw in link_text or kw in href_lower for kw in ADV_LINK_KEYWORDS):
            full_url = urljoin(base_url, href)
            parsed_link = urlparse(full_url)
            if parsed_link.scheme in ("http", "https"):
                return full_url
    return None


def _canonical_domain(url_or_domain: str) -> str:
    d = url_or_domain.strip().lower()
    if "://" not in d:
        d = "https://" + d
    host = urlparse(d).hostname or d
    return host[4:] if host.startswith("www.") else host


_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")


def _sembra_hostname_valido(valore: str) -> bool:
    """Il modello a volte scrive un commento dentro al campo dominio invece
    di un dominio pulito (es. "nordovestmilano.it (indicativo, verificare
    dominio ufficiale)") — osservato in test reale il 9/9/2026. urlparse
    non lo rifiuta: senza uno spazio o "://" espliciti, hostname resta None
    e si ricade sulla stringa originale intera, commento compreso, che poi
    finiva in un link "https://..." rotto nel frontend. Qui verifichiamo
    che il risultato di _canonical_domain abbia davvero la forma di un
    hostname (solo lettere/cifre/trattini ed etichette separate da punti)
    prima di fidarcene."""
    return bool(_HOSTNAME_RE.match(valore))


def _load_media_catalog() -> dict:
    if not MEDIA_CATALOG_FILE.exists():
        return {}
    try:
        with open(MEDIA_CATALOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_media_catalog(catalog: dict) -> None:
    with open(MEDIA_CATALOG_FILE, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)


def _verifica_advertising_evidence(dominio: str, catalog: dict) -> dict:
    """Ritorna {advertising_evidence, advertising_page, verification_status,
    last_verified}. Usa la cache del catalogo se il dominio è già stato
    verificato entro CATALOG_CACHE_DAYS — mai rifare una fetch inutile
    (istruzioni 9/9/2026, punto 26, "cache")."""
    canonical = _canonical_domain(dominio)
    cached = catalog.get(canonical)
    if cached and cached.get("last_verified"):
        try:
            from datetime import datetime, timezone
            eta_giorni = (datetime.now(timezone.utc) - datetime.fromisoformat(cached["last_verified"])).days
            if eta_giorni < CATALOG_CACHE_DAYS:
                return cached
        except Exception:
            pass

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    homepage = f"https://{canonical}"
    risultato = {
        "canonical_domain": canonical,
        "advertising_evidence": "UNKNOWN",
        "advertising_page": None,
        "verification_status": "UNVERIFIED",
        "last_verified": now_iso,
        "first_discovered": (cached or {}).get("first_discovered", now_iso),
    }
    if not _is_safe_url(homepage):
        return risultato
    try:
        html = _fetch_raw_html(homepage)
    except Exception:
        html = None
    if html is None:
        risultato["verification_status"] = "UNVERIFIED"
        return risultato

    risultato["verification_status"] = "PARTIALLY_VERIFIED"
    adv_link = _find_advertising_link(html, homepage)
    if adv_link:
        risultato["advertising_evidence"] = "HIGH"
        risultato["advertising_page"] = adv_link
        risultato["verification_status"] = "VERIFIED"
    else:
        # Home raggiunta ma nessun link pubblicità/media kit trovato in
        # homepage: non possiamo escludere che esista altrove, quindi resta
        # un'evidenza debole, non un "non vende pubblicità" (mai un fatto
        # negativo inventato).
        risultato["advertising_evidence"] = "LOW"

    return risultato


# ---------------------------------------------------------------------------
# Audience Opportunity — combinatore deterministico ed esplicito (mai una
# seconda chiamata AI per calcolarlo: sostituisce il vecchio "affinity
# score" percentuale con etichette qualitative, sempre spiegabili — vedi
# istruzioni 9/9/2026, punti 9 e 29 ("no false precision").
# ---------------------------------------------------------------------------
_LIVELLI = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0, "INSUFFICIENT_DATA": 0}


def _whitelist_fit(v, default="UNKNOWN") -> str:
    return v if v in ("HIGH", "MEDIUM", "LOW", "UNKNOWN") else default


def _calcola_audience_opportunity(geo_fit: str, context_fit: str, reader_intent_fit: str,
                                   business_fit: str, advertising_evidence: str) -> str:
    """HIGH/MEDIUM/LOW/INSUFFICIENT_DATA da geo+context+reader-intent+business
    fit (qualitativi, dall'AI) + evidenza pubblicitaria (verificata dal
    backend). Il budget fit NON entra qui: si applica dopo, separatamente
    (istruzioni, punto 15 — budget dopo la discovery, non prima)."""
    fits = [geo_fit, context_fit, reader_intent_fit, business_fit]
    conosciuti = [f for f in fits if f != "UNKNOWN"]
    if len(conosciuti) < 2:
        return "INSUFFICIENT_DATA"
    if geo_fit == "LOW":
        # Un territorio incompatibile resta un limite forte anche con tutto
        # il resto favorevole (stesso principio del fix ranking del 9/9).
        return "LOW"
    punteggio = sum(_LIVELLI[f] for f in fits) / len(fits)
    if advertising_evidence == "HIGH":
        punteggio += 0.3
    elif advertising_evidence == "UNKNOWN":
        punteggio -= 0.2
    if punteggio >= 2.3:
        return "HIGH"
    if punteggio >= 1.3:
        return "MEDIUM"
    return "LOW"


def _calcola_budget_fit(prezzo: Optional[dict], budget: int) -> str:
    """UNKNOWN se non abbiamo un prezzo verificato o stimato — MAI declassato
    a LOW solo perché non sappiamo il prezzo (istruzioni, punto 15)."""
    if not prezzo or prezzo.get("min") is None:
        return "UNKNOWN"
    if prezzo["min"] > budget * 1.10:
        return "LOW"
    if prezzo["min"] <= budget * 0.5:
        return "HIGH"
    return "MEDIUM"


def _calcola_verdetto(audience_opportunity: str, budget_fit: str, advertising_evidence: str) -> str:
    if audience_opportunity == "INSUFFICIENT_DATA":
        return "INVESTIGATE"
    if audience_opportunity == "LOW":
        return "DO_NOT_PRIORITIZE"
    if audience_opportunity == "HIGH" and budget_fit in ("HIGH", "MEDIUM") and advertising_evidence in ("HIGH", "MEDIUM"):
        return "CONTACT"
    if audience_opportunity in ("HIGH", "MEDIUM") and budget_fit == "LOW":
        return "INVESTIGATE"
    if audience_opportunity == "MEDIUM":
        return "CONSIDER"
    return "INVESTIGATE"


def _genera_domande_concessionaria(media_nome: str, azienda_region: Optional[str], budget: int, sector_label: str) -> list[str]:
    """Domande generate da un TEMPLATE (non un'altra chiamata AI: costo
    zero aggiuntivo — istruzioni, punto 19), ma personalizzate con i dati
    reali della ricerca corrente, non generiche uguali per tutti."""
    zona = azienda_region or "la tua zona"
    domande = [
        f"Che quota della vostra audience proviene da {zona}?",
        f"Avete formati pubblicitari geolocalizzabili su {zona}?",
        f"Qual è il costo indicativo di una campagna con un budget massimo di € {budget}?",
        f"Avete dati di performance o benchmark per inserzionisti del settore {sector_label}?",
        "Quali formati consigliate per generare contatti/preventivi, non solo visibilità?",
    ]
    return domande


@app.get("/api/fetch-site-summary")
def fetch_site_summary(url: str):
    """Legge davvero il sito indicato (dell'azienda o di un competitor) per
    dare all'AI un contesto reale invece di ragionare sul solo nome/URL.
    Se trova un link 'chi siamo'/'about' sullo stesso dominio, legge anche
    quello: la home da sola spesso è poco più di uno slogan. Fallisce in
    modo pulito (200 con 'ok': false) se il sito non è raggiungibile,
    reindirizza altrove, blocca lo scraping, o è generato via JavaScript
    (il server vede solo lo scheletro della pagina, non il contenuto reso
    dal browser) — il frontend prosegue senza quel contesto invece di far
    ragionare l'AI su testo vuoto o su un menu di navigazione."""
    if not _is_safe_url(url):
        return {"ok": False, "error": "URL non valido o non consentito"}
    try:
        html = _fetch_raw_html(url)
    except Exception:
        return {"ok": False, "error": "Sito non raggiungibile"}
    if html is None:
        return {"ok": False, "error": "Il sito ha risposto con un errore o un reindirizzamento non seguito per sicurezza"}

    title, text = _strip_html_text(html, max_chars=4000)

    # Prova la pagina "chi siamo"/"about" PRIMA di giudicare la home troppo
    # scarna per essere utile: è normalissimo che un'azienda tenga la home
    # quasi solo a slogan/hero visivo e la sostanza su una pagina interna —
    # bocciare qui perderebbe contenuto vero che è a un click di distanza.
    about_url = _find_about_link(html, url)
    if about_url and _is_safe_url(about_url):
        try:
            about_html = _fetch_raw_html(about_url)
        except Exception:
            about_html = None
        if about_html:
            _, about_text = _strip_html_text(about_html, max_chars=2500)
            if about_text:
                text = text + "\n\n[Pagina 'chi siamo'/'about']\n" + about_text

    # Solo ORA, dopo aver dato una chance alla pagina "chi siamo", un testo
    # ancora troppo scarno è più probabilmente un sito reso via JavaScript
    # (il server vede solo lo scheletro, il contenuto vero lo genera il
    # browser) che una pagina reale genuinamente minimale.
    if len(text) < 120:
        return {
            "ok": False,
            "error": "Il sito sembra generato via JavaScript: il contenuto non è leggibile senza eseguirlo in un browser",
        }

    return {"ok": True, "title": title, "testo": text[:6000]}


# ---------------------------------------------------------------------------
# Analisi AI — chiamata ad Anthropic SOLO da qui, mai dal browser.
#
# Prima di questo endpoint, site/taglio-demo.html chiamava
# https://api.anthropic.com/v1/messages direttamente dal client, senza
# nessuna chiave (funzionava solo dentro l'ambiente artifact di Claude.ai,
# che intercetta la richiesta e la autentica lui). Sul sito pubblico quella
# chiamata falliva sempre — vedi audit del 9/9/2026. Ora il browser chiama
# solo /api/generate-analysis; la chiave Anthropic vive solo in questo
# processo, letta da ANTHROPIC_API_KEY, mai scritta in un file del repo, mai
# rimandata al client, mai loggata.
# ---------------------------------------------------------------------------

MAX_SITE_TEXT_CHARS = 3000
MAX_COMPETITOR_TEXT_CHARS = 1800
MAX_COMPETITORS_IN_PROMPT = 5

# Limite in memoria, non distribuito: si azzera a ogni riavvio del processo.
# Non è un controllo di sicurezza, è un freno contro un loop lato client che
# altrimenti moltiplicherebbe il costo delle chiamate Anthropic — coerente
# con un prodotto a 4,99€/mese (vedi "COSTI" nelle istruzioni del 9/9).
_RATE_LIMIT_WINDOW_SEC = 3600
_RATE_LIMIT_MAX_REQUESTS = 10
_rate_limit_hits: dict = defaultdict(deque)


def _check_rate_limit(client_key: str) -> bool:
    now = time.monotonic()
    hits = _rate_limit_hits[client_key]
    while hits and now - hits[0] > _RATE_LIMIT_WINDOW_SEC:
        hits.popleft()
    if len(hits) >= _RATE_LIMIT_MAX_REQUESTS:
        return False
    hits.append(now)
    return True


class SiteSummaryIn(BaseModel):
    title: str = ""
    testo: str = ""


class GenerateAnalysisRequest(BaseModel):
    # Limiti di lunghezza sui campi liberi del wizard: non sono un controllo
    # di sicurezza in senso stretto (i dati arrivano dall'utente stesso, non
    # da terzi), ma evitano che un payload anomalo gonfi inutilmente il
    # prompt/costo della chiamata Anthropic (vedi istruzioni del 9/9, "limiti
    # di dimensione dell'input").
    #
    # Questo oggetto è la SINGLE SOURCE OF TRUTH di una ricerca (istruzioni
    # 9/9/2026, "Taglio 3.0", punto 1): il frontend lo ricostruisce da zero
    # ad ogni analisi, mai riusando budget/competitor/zona di una ricerca
    # precedente — vedi search_id.
    model_config = ConfigDict(populate_by_name=True)
    search_id: str = Field("", max_length=100)
    name: str = Field("la tua azienda", max_length=200)
    website: str = Field("", max_length=500)
    prodotto: str = Field("", max_length=500)
    zona_geografica: str = Field("", alias="zonaGeografica", max_length=200)
    target_cliente: str = Field("", alias="targetCliente", max_length=500)
    sector_label: str = Field("il tuo settore", alias="sectorLabel", max_length=100)
    sector_key: str = Field("", alias="sectorKey", max_length=30)  # chiave tassonomia 7-settori, per filtrare il catalogo (come /api/allocation)
    customer_type: str = Field("", alias="customerType", max_length=20)  # B2C|B2B|BOTH|"" (non indicato)
    tone: str = Field("", max_length=100)
    competitors: list[str] = Field(default_factory=list, max_length=10)
    budget: int = Field(0, ge=0, le=10_000_000)
    period_key: str = Field("1mese", alias="periodKey", max_length=20)
    obiettivo: str = Field("", max_length=100)
    # Free vs Plus (istruzioni 9/9/2026, punto 24): il backend è l'unica
    # fonte di verità sui limiti mostrati, mai un valore deciso solo lato
    # frontend — ma qui ci fidiamo del flag solo per la PROFONDITÀ della
    # ricerca (quanti media scoprire/mostrare), non per funzioni a
    # pagamento sensibili (Stripe/Supabase restano l'unica fonte di verità
    # sull'abbonamento vero).
    is_plus: bool = Field(False, alias="isPlus")
    # Riusa il contenuto già letto dal frontend via /api/fetch-site-summary:
    # non rileggiamo lo stesso sito una seconda volta da qui (vedi istruzioni
    # del 9/9, "non effettuare una seconda lettura inutile").
    sito_azienda: Optional[SiteSummaryIn] = Field(None, alias="sitoAzienda")
    siti_competitor: list[SiteSummaryIn] = Field(default_factory=list, alias="sitiCompetitor", max_length=10)


def _truncate(s: str, n: int) -> str:
    return (s or "")[:n]


# Controllo costi (istruzioni 9/9/2026, punto 25): quanti candidati chiedere
# all'AI in una singola chiamata. Più alto per Plus (punto 24 — "tutte le
# opportunità, catalogo esteso, più verifiche approfondite"), ma sempre un
# tetto esplicito, mai illimitato.
MAX_MEDIA_DISCOVERED_FREE = 10
MAX_MEDIA_DISCOVERED_PLUS = 16


def _build_analysis_prompt(payload: GenerateAnalysisRequest) -> tuple[str, bool, bool, int]:
    max_discovery = MAX_MEDIA_DISCOVERED_PLUS if payload.is_plus else MAX_MEDIA_DISCOVERED_FREE
    sito_block = ""
    if payload.sito_azienda and payload.sito_azienda.testo:
        sito_block = (
            f"\nContenuto reale letto dal sito dell'azienda (titolo: \"{payload.sito_azienda.title}\"):\n"
            f"\"\"\"{_truncate(payload.sito_azienda.testo, MAX_SITE_TEXT_CHARS)}\"\"\"\n"
        )

    competitor_testi = [c for c in payload.siti_competitor if c.testo][:MAX_COMPETITORS_IN_PROMPT]
    competitor_block = ""
    if competitor_testi:
        parti = [
            f"Contenuto reale letto dal sito del competitor {i + 1} (titolo: \"{c.title}\"):\n"
            f"\"\"\"{_truncate(c.testo, MAX_COMPETITOR_TEXT_CHARS)}\"\"\""
            for i, c in enumerate(competitor_testi)
        ]
        competitor_block = "\n" + "\n\n".join(parti) + "\n"

    ha_sito_reale = bool(sito_block)
    ha_competitor_reale = bool(competitor_block)

    if ha_sito_reale:
        istruzioni_fonte = (
            "Hai contenuto REALE letto dal sito dell'azienda: usalo come base primaria per il profilo "
            "azienda. Per ogni campo del profilo indica \"stato\": \"RILEVATO\" se è scritto esplicitamente "
            "sul sito, \"DEDUZIONE\" se è una deduzione ragionevole ma non dichiarata esplicitamente, "
            "\"NON_DETERMINABILE\" se non hai abbastanza elementi — in quel caso lascia \"testo\" vuoto o "
            "con una frase che dichiara l'assenza del dato, MAI un'invenzione."
        )
    else:
        istruzioni_fonte = (
            "NON hai contenuto reale del sito dell'azienda (non indicato, non raggiungibile, o generato via "
            "JavaScript quindi illeggibile). Ogni campo del profilo azienda deve avere \"stato\": "
            "\"NON_DETERMINABILE\" con \"testo\" vuoto, tranne al massimo un campo dedotto SOLO da settore/"
            "prodotto/zona indicati esplicitamente dall'utente nel wizard (in quel caso \"stato\": "
            "\"DEDUZIONE\"). Non inventare mai un'attività, un'area di mercato o un cliente che non hai modo "
            "di sapere."
        )

    # Le testate già nel catalogo (config.yaml) NON sono un universo chiuso,
    # ma l'AI le deve comunque considerare come candidate a pieno titolo
    # insieme a quelle scoperte via ricerca web — istruzioni 9/9/2026,
    # "Taglio 3.0": "le 67 testate esistenti diventano parte del catalogo,
    # non l'universo entro cui scegliere".
    testate_catalogo = _load_testate_config()
    allowed = categorie_rilevanti(payload.sector_key) if payload.sector_key else None
    nomi_catalogo = sorted(testate_catalogo.keys()) if not allowed else sorted(
        nome for nome, cats in testate_catalogo.items() if set(cats) & allowed
    )

    prompt = f"""Sei il consulente pubblicitario AI di Taglio. Il tuo compito non è scegliere la testata migliore da un elenco fisso: è capire DAVVERO questa azienda e il suo cliente potenziale, poi cercare dove — editorialmente — avrebbe senso cercare quel pubblico, includendo media che potrebbero non essere ancora nel nostro catalogo.

Dati azienda:
Nome: {payload.name}
Sito web: {payload.website or 'non indicato'}
Prodotto o servizio specifico: {payload.prodotto or 'non indicato'}
Zona geografica dichiarata dall'utente: {payload.zona_geografica or 'non indicata — deducila dal sito se possibile'}
Target di clientela dichiarato: {payload.target_cliente or 'non indicato'}
Tipo cliente dichiarato: {payload.customer_type or 'non indicato — deducilo se possibile'}
Settore scelto nel wizard (può essere generico, non vincolarti): {payload.sector_label}
Tono di marca: {payload.tone or 'non specificato'}
Competitor indicati: {', '.join(payload.competitors) if payload.competitors else 'nessuno'}
Budget indicativo: € {payload.budget} per il periodo scelto
Obiettivo campagna: {payload.obiettivo or 'non indicato'}
{sito_block}{competitor_block}
{istruzioni_fonte}

REGOLA FONDAMENTALE, vale per OGNI sezione: non inventare mai clienti, recensioni, fatturato, audience, diffusione, CPM, prezzi ufficiali, certificazioni, partnership, sconti, anni di garanzia, numero di clienti, percentuali di risparmio, risultati di campagne, o dati di audience non trovati davvero. Se un dato non è rilevabile, dichiaralo esplicitamente invece di inventarlo.

Determina il raggio d'azione geografico REALE dell'azienda:
- "market_scope": "LOCAL" (città/provincia), "REGIONAL" (regione), "NATIONAL" (Italia), "INTERNATIONAL", "UNKNOWN"
- "market_region": regione italiana se LOCAL/REGIONAL, altrimenti null
- "market_city": città se LOCAL, altrimenti null
- "market_confidence": "HIGH" se dichiarato esplicitamente, "MEDIUM" se dedotto, "LOW" se ipotesi debole. Senza indizi: "UNKNOWN"/null/"LOW".
- "business_model": "B2C", "B2B", "BOTH", o "UNKNOWN"

Genera un oggetto "azienda" con questi campi, ciascuno {{"testo":"...","stato":"RILEVATO|DEDUZIONE|NON_DETERMINABILE"}}: "attivita", "area_mercato", "cliente_probabile", "leva_commerciale", "call_to_action", più "subsector" (sotto-settore specifico, es. "ristrutturazioni residenziali" non solo "edilizia") e "punti_distintivi" come ARRAY di massimo 3 oggetti {{"testo":"...","stato":"..."}}.
Genera anche "main_products_services" (ARRAY di stringhe, prodotti/servizi principali realmente offerti) e "customer_intents" (ARRAY di stringhe, cosa sta cercando di fare il cliente potenziale quando ha bisogno di questa azienda, es. "ristrutturare il bagno", "cambiare i serramenti" — SOLO se deducibile).

{"Hai anche contenuto REALE del sito di un competitor. Genera \"competitor_confronto\": {\"disponibile\":true,\"tu_comunichi_meglio\":[\"...\"],\"competitor_comunica_meglio\":[\"...\"],\"opportunita\":[\"...\"],\"messaggio_da_possedere\":\"...\"} — differenze COMMERCIALMENTE UTILI, basate solo su ciò che i due siti dicono davvero." if ha_competitor_reale else "Non hai contenuto reale di un competitor: genera \"competitor_confronto\": {\"disponibile\":false}."}

MEDIA STRATEGY — prima di cercare nomi di testate, ragiona su DOVE potrebbe trovarsi editorialmente il cliente potenziale di questa azienda specifica (non una lista generica uguale per tutti i settori). Genera "media_strategy": ARRAY di oggetti {{"category":"...","reason":"...","priority":"HIGH|MEDIUM|LOW","evidence":"..."}} — 3-6 categorie pertinenti a QUESTA azienda.

MEDIA DISCOVERY — usa la ricerca web per trovare fino a {max_discovery} media (quotidiani, quotidiani online, settimanali, periodici, free press, magazine verticali, portali editoriali, media locali o professionali) realmente pertinenti al profilo sopra. NON limitarti a nomi noti o ovvi: cerca davvero. NON includere marketplace, directory, piattaforme di lead generation, social network o motori di ricerca come se fossero media editoriali — se ne trovi uno pertinente, includilo con "channel_type":"MARKETPLACE|DIRECTORY|LEAD_GEN|SOCIAL|SEARCH_ENGINE" invece di "MEDIA", verrà trattato separatamente.
{"Considera anche, se pertinenti, queste testate già nel nostro catalogo: " + ', '.join(nomi_catalogo[:60]) + "." if nomi_catalogo else ""}

Per ogni media (di catalogo o nuovo) genera un oggetto in "media_discovered": {{"nome":"...","dominio":"...","tipo":"quotidiano|quotidiano_online|settimanale|periodico|free_press|magazine|verticale|portale|altro","channel_type":"MEDIA|MARKETPLACE|DIRECTORY|LEAD_GEN|SOCIAL|SEARCH_ENGINE","geographic_scope":"LOCAL|REGIONAL|NATIONAL|UNKNOWN","region":"..." o null,"topics":["...","..."],"geo_fit":"HIGH|MEDIUM|LOW|UNKNOWN","context_fit":"HIGH|MEDIUM|LOW|UNKNOWN","reader_intent_fit":"HIGH|MEDIUM|LOW|UNKNOWN","business_fit":"HIGH|MEDIUM|LOW|UNKNOWN","perche":"1-3 ragioni concrete, non generiche"}}.
- "geo_fit": confronta il territorio del media con quello dell'azienda — LOW se palesemente incompatibile (es. azienda locale Lombardia + media regionale Sardegna), indipendentemente da quanti dati abbiamo.
- "context_fit": quanto gli argomenti/sezioni editoriali del media sono coerenti col bisogno del cliente potenziale (es. media dedicato a ristrutturazione + azienda di ristrutturazioni → HIGH).
- "reader_intent_fit": inferenza dal CONTESTO editoriale (non dati reali di comportamento lettori) su quanto chi legge quel media potrebbe avere l'intento d'acquisto del cliente tipo.
- "business_fit": coerenza col business_model (B2C/B2B) — un media per professionisti è business_fit basso per un'azienda B2C locale, e viceversa.
- Se non hai abbastanza informazioni per un campo, usa "UNKNOWN", mai un'invenzione.

Genera "messaggio_pubblicitario": {{"headline":"...","sottoheadline":"...","cta":"...","argomento_principale":"...","prova_fatto":"..."}} basato SOLO su ciò che è realmente disponibile.

Genera "creativita" con tre livelli ({{"titolo":"...","perche":"...","dove":"...","messaggio":"...","rischio":"..."}}): "consigliata" (basso rischio, coerente), "alternativa" (più distintiva), "audace" (meccanismo da altro settore).

Rispondi SOLO con un oggetto JSON valido, nessun testo prima o dopo, con esattamente queste chiavi: analisi_azienda, market_scope, market_region, market_city, market_confidence, business_model, azienda, main_products_services, customer_intents, competitor_confronto, media_strategy, media_discovered, messaggio_pubblicitario, creativita. Scrivi tutti i testi in italiano."""

    return prompt, ha_sito_reale, ha_competitor_reale, max_discovery


@app.post("/api/generate-analysis")
def generate_analysis(payload: GenerateAnalysisRequest, request: Request):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Analisi AI non ancora configurata (manca la chiave Anthropic sul server).",
        )

    client_key = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_key):
        raise HTTPException(status_code=429, detail="Troppe richieste di analisi in poco tempo. Riprova tra qualche minuto.")

    prompt, ha_sito_reale, ha_competitor_reale, max_discovery = _build_analysis_prompt(payload)

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                # Lo schema Taglio 3.0 (profilo azienda esteso + media
                # strategy + fino a 16 media scoperti per un utente Plus,
                # ciascuno con più campi di fit + motivazione, + 3 varianti
                # di creatività) produce una risposta molto lunga — con
                # 8000 alcune risposte reali (soprattutto lato Plus, con più
                # media da descrivere) si troncavano a metà JSON e fallivano
                # il parsing lato server (osservato in test reale il
                # 9/9/2026, ~1 chiamata su 3). Margine ampio qui perché il
                # costo è comunque per singola ricerca (vedi "cost control").
                "max_tokens": 12000,
                "thinking": {"type": "disabled"},
                # Ricerca web reale per la Media Discovery (istruzioni
                # 9/9/2026, "Taglio 3.0", punto 4) — non nomi a memoria del
                # modello, ma dominii verificabili. max_uses tiene sotto
                # controllo il costo per ricerca (punto 25, "cost control");
                # il backend verifica comunque ogni dominio con una fetch
                # reale prima di considerarlo attendibile (mai fiducia cieca
                # nella ricerca del modello).
                "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
                "messages": [{"role": "user", "content": prompt}],
            },
            # Con fino a 6 ricerche web reali + uno schema di risposta esteso,
            # le chiamate riuscite osservate in produzione durano regolarmente
            # 80-100s: con timeout=90 una parte di richieste legittime (non
            # bloccate, solo lente) veniva interrotta e mostrata come "AI non
            # ha risposto in tempo" — osservato in test reale il 9/9/2026.
            timeout=170,
        )
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="Il motore AI non ha risposto in tempo. Riprova.")
    except requests.exceptions.RequestException:
        raise HTTPException(status_code=502, detail="Impossibile contattare il motore AI. Riprova più tardi.")

    if resp.status_code != 200:
        # Mai esporre il corpo grezzo della risposta di Anthropic al client:
        # solo un messaggio generico, i dettagli restano nei log del server.
        raise HTTPException(status_code=502, detail="Il motore AI ha risposto con un errore. Riprova più tardi.")

    data: dict = {}
    text = ""
    try:
        data = resp.json()
        # Con il tool di ricerca web, "content" contiene anche blocchi
        # server_tool_use/web_search_tool_result intercalati: prendiamo
        # solo i blocchi di testo, nell'ordine in cui arrivano (il JSON
        # finale è nell'ultimo/unico blocco "text").
        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        # Con il tool di ricerca web attivo il modello a volte antepone una
        # breve frase di transizione prima del JSON, o ne aggiunge una dopo
        # (es. "Ho raccolto abbastanza informazioni...", oppure un commento
        # finale) nonostante l'istruzione di rispondere SOLO con JSON —
        # osservato in test reale il 9/9/2026. Un regex "greedy" dal primo
        # '{' all'ultimo '}' del testo si era rivelato fragile: qualunque
        # graffa in un commento finale rompeva il parsing. _estrai_oggetto_
        # json conta la profondità delle graffe e si ferma alla chiusura
        # corretta, indipendentemente da cosa segue nel testo.
        oggetto = _estrai_oggetto_json(text)
        if oggetto is None:
            raise ValueError("nessun blocco JSON bilanciato trovato nella risposta")
        parsed = json.loads(oggetto)
    except Exception as exc:
        # Diagnostica solo nei log del server (mai al client): stop_reason
        # dice se il modello si è fermato per max_tokens (JSON troncato) o
        # per fine naturale con un formato inatteso — differenza osservata
        # in test reale il 9/9/2026 (~1 chiamata su 3 falliva così).
        logger.error(
            "generate-analysis: parsing JSON fallito (%s: %s) — stop_reason=%s output_tokens=%s coda_testo=%r",
            type(exc).__name__, exc,
            data.get("stop_reason"), data.get("usage", {}).get("output_tokens"), text[-500:],
        )
        raise HTTPException(status_code=502, detail="Risposta AI in un formato inatteso. Riprova.")

    if not isinstance(parsed, dict):
        raise HTTPException(status_code=502, detail="Risposta AI in un formato inatteso. Riprova.")

    # Il raggio d'azione geografico dedotto dall'AI diventa un input
    # strutturato per il motore di allocazione (buildAllocationPlan nel
    # frontend), non solo testo descrittivo — vedi istruzioni del 9/9/2026,
    # correzione ranking punto 6. Whitelist rigida: mai fidarsi di un valore
    # libero del modello per un campo poi usato in una logica di esclusione.
    market_scope = parsed.get("market_scope")
    if market_scope not in ("LOCAL", "REGIONAL", "NATIONAL", "UNKNOWN"):
        market_scope = "UNKNOWN"
    market_confidence = parsed.get("market_confidence")
    if market_confidence not in ("HIGH", "MEDIUM", "LOW"):
        market_confidence = "LOW"
    market_region = parsed.get("market_region")
    if not isinstance(market_region, str) or not market_region.strip() or market_scope not in ("LOCAL", "REGIONAL"):
        market_region = None
    market_city = parsed.get("market_city")
    if not isinstance(market_city, str) or not market_city.strip() or market_scope != "LOCAL":
        market_city = None
    business_model = parsed.get("business_model")
    if business_model not in ("B2C", "B2B", "BOTH"):
        business_model = "UNKNOWN"

    def _campo(v) -> dict:
        """Normalizza un campo {testo, stato}: mai un'invenzione strutturale
        anche se il modello sbaglia forma — un campo malformato diventa
        NON_DETERMINABILE con testo vuoto, mai un testo a caso."""
        if isinstance(v, dict) and isinstance(v.get("testo"), str):
            stato = v.get("stato") if v.get("stato") in ("RILEVATO", "DEDUZIONE", "NON_DETERMINABILE") else "DEDUZIONE"
            testo = v["testo"].strip()
            if not testo:
                stato = "NON_DETERMINABILE"
            return {"testo": testo, "stato": stato}
        if isinstance(v, str) and v.strip():
            return {"testo": v.strip(), "stato": "DEDUZIONE"}
        return {"testo": "", "stato": "NON_DETERMINABILE"}

    azienda_raw = parsed.get("azienda") if isinstance(parsed.get("azienda"), dict) else {}
    punti_raw = azienda_raw.get("punti_distintivi")
    punti_distintivi = [_campo(p) for p in punti_raw][:3] if isinstance(punti_raw, list) else []
    azienda = {
        "attivita": _campo(azienda_raw.get("attivita")),
        "area_mercato": _campo(azienda_raw.get("area_mercato")),
        "subsector": _campo(azienda_raw.get("subsector")),
        "cliente_probabile": _campo(azienda_raw.get("cliente_probabile")),
        "leva_commerciale": _campo(azienda_raw.get("leva_commerciale")),
        "call_to_action": _campo(azienda_raw.get("call_to_action")),
        "punti_distintivi": punti_distintivi,
    }

    def _lista_stringhe(v, max_n=6) -> list[str]:
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if isinstance(x, (str, int, float)) and str(x).strip()][:max_n]

    main_products_services = _lista_stringhe(parsed.get("main_products_services"))
    customer_intents = _lista_stringhe(parsed.get("customer_intents"))

    # Il confronto competitor è forzato lato server in base a cosa abbiamo
    # DAVVERO passato al modello (non a cosa il modello dichiara): se non
    # avevamo testo reale di un competitor, il confronto non può esistere,
    # a prescindere da cosa il modello ha provato a generare.
    if ha_competitor_reale and isinstance(parsed.get("competitor_confronto"), dict):
        cc_raw = parsed["competitor_confronto"]
        def _lista(v) -> list[str]:
            if not isinstance(v, list):
                return []
            return [str(x).strip() for x in v if isinstance(x, (str, int, float)) and str(x).strip()][:3]
        competitor_confronto = {
            "disponibile": True,
            "tu_comunichi_meglio": _lista(cc_raw.get("tu_comunichi_meglio")),
            "competitor_comunica_meglio": _lista(cc_raw.get("competitor_comunica_meglio")),
            "opportunita": _lista(cc_raw.get("opportunita")),
            "messaggio_da_possedere": cc_raw.get("messaggio_da_possedere") if isinstance(cc_raw.get("messaggio_da_possedere"), str) else "",
        }
    else:
        competitor_confronto = {"disponibile": False}

    mp_raw = parsed.get("messaggio_pubblicitario") if isinstance(parsed.get("messaggio_pubblicitario"), dict) else {}
    def _testo(v) -> str:
        return v.strip() if isinstance(v, str) else ""
    messaggio_pubblicitario = {
        "headline": _testo(mp_raw.get("headline")),
        "sottoheadline": _testo(mp_raw.get("sottoheadline")),
        "cta": _testo(mp_raw.get("cta")),
        "argomento_principale": _testo(mp_raw.get("argomento_principale")),
        "prova_fatto": _testo(mp_raw.get("prova_fatto")),
    }

    def _idea(v) -> dict:
        if not isinstance(v, dict):
            v = {}
        return {
            "titolo": _testo(v.get("titolo")),
            "perche": _testo(v.get("perche")),
            "dove": _testo(v.get("dove")),
            "messaggio": _testo(v.get("messaggio")),
            "rischio": _testo(v.get("rischio")),
        }

    creativita_raw = parsed.get("creativita") if isinstance(parsed.get("creativita"), dict) else {}
    creativita = {
        "consigliata": _idea(creativita_raw.get("consigliata")),
        "alternativa": _idea(creativita_raw.get("alternativa")),
        "audace": _idea(creativita_raw.get("audace")),
    }

    if not creativita["consigliata"]["titolo"]:
        raise HTTPException(status_code=502, detail="Risposta AI incompleta. Riprova.")

    # Media strategy: le categorie devono venire dal profilo di QUESTA
    # azienda, non da una lista fissa per settore (istruzioni, punto 3).
    media_strategy_raw = parsed.get("media_strategy")
    media_strategy = []
    if isinstance(media_strategy_raw, list):
        for m in media_strategy_raw[:8]:
            if not isinstance(m, dict) or not m.get("category"):
                continue
            priority = m.get("priority") if m.get("priority") in ("HIGH", "MEDIUM", "LOW") else "MEDIUM"
            media_strategy.append({
                "category": str(m["category"])[:120],
                "reason": _testo(m.get("reason"))[:300],
                "priority": priority,
                "evidence": _testo(m.get("evidence"))[:300],
            })

    # ------------------------------------------------------------------
    # Media discovered: normalizzazione + deduplica per dominio canonico +
    # incrocio col catalogo esistente + verifica pubblicitaria reale per i
    # media davvero nuovi + calcolo di Audience Opportunity/Budget Fit/
    # Verdetto (istruzioni 9/9/2026, "Taglio 3.0", punti 6-18).
    # ------------------------------------------------------------------
    testate_urls = _load_testate_urls()
    dominio_to_nome_catalogo = {v: k for k, v in testate_urls.items()}
    aggregated_by_nome = {}
    if AGGREGATED_FILE.exists():
        with open(AGGREGATED_FILE, "r", encoding="utf-8") as f:
            for row in json.load(f):
                aggregated_by_nome[row["nome"]] = row

    media_catalog_cache = _load_media_catalog()
    media_grezzi = parsed.get("media_discovered")
    media_normalizzati = []
    visti_domini = set()
    if isinstance(media_grezzi, list):
        for m in media_grezzi[:max_discovery]:
            if not isinstance(m, dict) or not m.get("nome") or not m.get("dominio"):
                continue
            try:
                canonical = _canonical_domain(str(m["dominio"]))
            except Exception:
                continue
            if not canonical or not _sembra_hostname_valido(canonical) or canonical in visti_domini:
                continue
            visti_domini.add(canonical)
            media_normalizzati.append({
                "nome": str(m["nome"])[:150],
                "dominio": canonical,
                "tipo": str(m.get("tipo") or "altro")[:40],
                "channel_type": m.get("channel_type") if m.get("channel_type") in
                    ("MEDIA", "MARKETPLACE", "DIRECTORY", "LEAD_GEN", "SOCIAL", "SEARCH_ENGINE") else "MEDIA",
                "geographic_scope": _whitelist_fit(m.get("geographic_scope"), "UNKNOWN") if m.get("geographic_scope") in ("LOCAL", "REGIONAL", "NATIONAL") else "UNKNOWN",
                "region": str(m["region"])[:60] if isinstance(m.get("region"), str) and m.get("region", "").strip() else None,
                "topics": _lista_stringhe(m.get("topics"), 5),
                "geo_fit": _whitelist_fit(m.get("geo_fit")),
                "context_fit": _whitelist_fit(m.get("context_fit")),
                "reader_intent_fit": _whitelist_fit(m.get("reader_intent_fit")),
                "business_fit": _whitelist_fit(m.get("business_fit")),
                "perche": _testo(m.get("perche"))[:400],
            })

    media_editoriali = []
    canali_alternativi = []
    for m in media_normalizzati:
        if m["channel_type"] != "MEDIA":
            # Marketplace/directory/lead-gen/social/motori di ricerca:
            # conservati ma MAI dentro il ranking editoriale (istruzioni,
            # punto 5 — "non deve entrare automaticamente nel ranking").
            canali_alternativi.append({"nome": m["nome"], "dominio": m["dominio"], "channel_type": m["channel_type"], "perche": m["perche"]})
            continue

        nome_catalogo = dominio_to_nome_catalogo.get(m["dominio"])
        if nome_catalogo:
            # Fonte: catalogo — usa la geo-classificazione già validata e i
            # dati reali di scraping come segnale bonus, mai come requisito
            # (istruzioni, punto 13).
            scope_cat, regione_cat = _classifica_geo_testata_catalogo(nome_catalogo)
            m["geographic_scope"] = scope_cat
            m["region"] = regione_cat
            m["fonte"] = "catalogo"
            m["verification_status"] = "VERIFIED"
            riga = aggregated_by_nome.get(nome_catalogo)
            if riga:
                m["advertising_evidence"] = "HIGH" if (riga.get("segnali_osservati") or 0) > 0 else "MEDIUM"
                m["advertising_page"] = riga.get("contatto_pubblicitario_url")
                m["segnale_competitivo"] = {
                    "stato": "PROBABLE" if (riga.get("segnali_osservati") or 0) > 0 else "UNKNOWN",
                    "testo": (f"Nelle nostre rilevazioni abbiamo osservato {riga['segnali_osservati']} segnali "
                              f"pubblicitari su questa testata (ultima rilevazione: {riga.get('ultima_osservazione') or 'n/d'})."
                              if (riga.get("segnali_osservati") or 0) > 0 else
                              "Nelle rilevazioni disponibili non abbiamo identificato advertiser comparabili."),
                }
                m["prezzo"] = _stima_prezzo_catalogo(nome_catalogo, riga.get("formato"), riga.get("formato_categoria"), payload.period_key)
                m["ultima_rilevazione"] = riga.get("ultima_osservazione")
            else:
                m["advertising_evidence"] = "UNKNOWN"
                m["advertising_page"] = None
                m["segnale_competitivo"] = {"stato": "UNKNOWN", "testo": "Nessuna rilevazione ancora disponibile per questa testata."}
                m["prezzo"] = {"tier": "PRICE_ON_REQUEST", "min": None, "max": None, "fonte": "Prezzo da richiedere alla concessionaria."}
                m["ultima_rilevazione"] = None
        else:
            # Fonte: discovery — verifica reale (fetch + cache), mai un
            # prezzo (nessuna fascia nota per un media appena scoperto).
            verifica = _verifica_advertising_evidence(m["dominio"], media_catalog_cache)
            media_catalog_cache[m["dominio"]] = verifica
            m["fonte"] = "discovery"
            m["verification_status"] = verifica["verification_status"]
            m["advertising_evidence"] = verifica["advertising_evidence"]
            m["advertising_page"] = verifica["advertising_page"]
            m["segnale_competitivo"] = {"stato": "UNKNOWN", "testo": "Media appena scoperto: nessuna rilevazione storica ancora disponibile."}
            m["prezzo"] = {"tier": "PRICE_ON_REQUEST", "min": None, "max": None, "fonte": "Prezzo da richiedere alla concessionaria."}
            m["ultima_rilevazione"] = None

        m["audience_opportunity"] = _calcola_audience_opportunity(
            m["geo_fit"], m["context_fit"], m["reader_intent_fit"], m["business_fit"], m["advertising_evidence"]
        )
        m["budget_fit"] = _calcola_budget_fit(m["prezzo"], payload.budget)
        m["verdetto"] = _calcola_verdetto(m["audience_opportunity"], m["budget_fit"], m["advertising_evidence"])
        m["data_confidence"] = "HIGH" if m["fonte"] == "catalogo" and m.get("ultima_rilevazione") else (
            "MEDIUM" if m["verification_status"] in ("VERIFIED", "PARTIALLY_VERIFIED") else "LOW")
        media_editoriali.append(m)

    _save_media_catalog(media_catalog_cache)

    # Ordinamento: PRIMA per Audience Opportunity (la qualità reale
    # dell'opportunità), poi per verdetto come criterio secondario. Un
    # budget_fit UNKNOWN (prezzo non noto, comune per un media appena
    # scoperto) porta quasi sempre a verdetto INVESTIGATE anche con
    # Audience Opportunity HIGH — se il verdetto pesasse più dell'Audience
    # Opportunity nell'ordinamento, un'opportunità HIGH-ma-da-approfondire
    # finirebbe sotto una MEDIA-ma-contattabile: un bug reale trovato in
    # test il 9/9/2026 (vedi "Taglio 3.0", punto 18 — "le migliori
    # opportunità" deve riflettere la qualità, non solo l'azionabilità
    # immediata). Mai un punteggio decimale mostrato: l'ordine è solo
    # interno, per scegliere il top 5.
    _ORDINE_VERDETTO = {"CONTACT": 3, "CONSIDER": 2, "INVESTIGATE": 1, "DO_NOT_PRIORITIZE": 0}
    media_editoriali.sort(key=lambda m: (_LIVELLI.get(m["audience_opportunity"], 0), _ORDINE_VERDETTO[m["verdetto"]]), reverse=True)

    # Free vs Plus (istruzioni, punto 24): Free mostra comunque un risultato
    # utile e completo (non impoverito ad arte), Plus mostra tutte le
    # opportunità trovate nel campione più ampio già scoperto sopra.
    tetto_opportunita = 8 if payload.is_plus else 5
    tetto_approfondire = 8 if payload.is_plus else 3
    opportunita_migliori = [m for m in media_editoriali if m["verdetto"] != "DO_NOT_PRIORITIZE"][:tetto_opportunita]
    da_approfondire = [m for m in media_editoriali if m["verdetto"] in ("INVESTIGATE", "CONSIDER") and m not in opportunita_migliori][:tetto_approfondire]
    # "Dove non investirei": SOLO ragioni reali (geo/contesto/budget), MAI
    # solo perché "pochi dati" (istruzioni, punto 21).
    dove_non_investirei = [
        m for m in media_editoriali
        if m["verdetto"] == "DO_NOT_PRIORITIZE" and (m["geo_fit"] == "LOW" or m["context_fit"] == "LOW" or m["budget_fit"] == "LOW")
    ][:3]

    azienda_regione_per_domande = market_region or market_city
    # Se l'utente non ha scelto un settore nel wizard, usa il sotto-settore
    # dedotto dall'AI (es. "ristrutturazioni residenziali") invece di una
    # stringa vuota — altrimenti la domanda diventa "inserzionisti del
    # settore ?" (bug osservato in test il 9/9/2026 col caso NM Edilizia).
    settore_per_domande = payload.sector_label if payload.sector_label and payload.sector_label != "il tuo settore" else (
        azienda["subsector"]["testo"] or "questo settore"
    )
    for m in opportunita_migliori:
        if m["verdetto"] in ("CONTACT", "INVESTIGATE"):
            m["domande_concessionaria"] = _genera_domande_concessionaria(m["nome"], azienda_regione_per_domande, payload.budget, settore_per_domande)
        else:
            m["domande_concessionaria"] = []

    # FREE mostra solo il messaggio pubblicitario base e l'idea consigliata;
    # PLUS sblocca anche l'alternativa e l'audace (istruzioni, punto 24 —
    # "tutte le creatività/varianti" solo per Plus). Nessun dato tolto,
    # solo non generato nel risultato finale per chi non è abbonato.
    creativita_risposta = {"consigliata": creativita["consigliata"]}
    if payload.is_plus:
        creativita_risposta["alternativa"] = creativita["alternativa"]
        creativita_risposta["audace"] = creativita["audace"]

    return {
        "analisi_azienda": parsed.get("analisi_azienda", "") if isinstance(parsed.get("analisi_azienda"), str) else "",
        "market_scope": market_scope,
        "market_region": market_region,
        "market_city": market_city,
        "market_confidence": market_confidence,
        "business_model": business_model,
        "azienda": azienda,
        "main_products_services": main_products_services,
        "customer_intents": customer_intents,
        "competitor_confronto": competitor_confronto,
        "media_strategy": media_strategy,
        "opportunita_migliori": opportunita_migliori,
        "media_da_approfondire": da_approfondire,
        "dove_non_investirei": dove_non_investirei,
        "canali_alternativi": canali_alternativi,
        "messaggio_pubblicitario": messaggio_pubblicitario,
        "creativita": creativita_risposta,
        "is_plus": payload.is_plus,
        "sito_letto_davvero": ha_sito_reale,
    }
