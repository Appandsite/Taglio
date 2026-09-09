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

from taxonomy import categorie_rilevanti

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
    model_config = ConfigDict(populate_by_name=True)
    name: str = Field("la tua azienda", max_length=200)
    website: str = Field("", max_length=500)
    prodotto: str = Field("", max_length=500)
    zona_geografica: str = Field("", alias="zonaGeografica", max_length=200)
    target_cliente: str = Field("", alias="targetCliente", max_length=500)
    sector_label: str = Field("il tuo settore", alias="sectorLabel", max_length=100)
    tone: str = Field("", max_length=100)
    competitors: list[str] = Field(default_factory=list, max_length=10)
    budget: int = Field(0, ge=0, le=10_000_000)
    obiettivo: str = Field("", max_length=100)
    # Riusa il contenuto già letto dal frontend via /api/fetch-site-summary:
    # non rileggiamo lo stesso sito una seconda volta da qui (vedi istruzioni
    # del 9/9, "non effettuare una seconda lettura inutile").
    sito_azienda: Optional[SiteSummaryIn] = Field(None, alias="sitoAzienda")
    siti_competitor: list[SiteSummaryIn] = Field(default_factory=list, alias="sitiCompetitor", max_length=10)


def _truncate(s: str, n: int) -> str:
    return (s or "")[:n]


def _build_analysis_prompt(payload: GenerateAnalysisRequest) -> tuple[str, bool]:
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

    ha_sito_reale = bool(sito_block or competitor_block)

    if ha_sito_reale:
        istruzioni_fonte = (
            "Hai contenuto REALE letto da almeno un sito (azienda e/o competitor) qui sopra: usalo come "
            "base primaria del ragionamento. Ogni consiglio che deriva da qualcosa scritto davvero su quel "
            "sito va etichettato \"FACT\". Un consiglio dedotto ragionevolmente ma non scritto esplicitamente "
            "va etichettato \"INFERENCE\". Una proposta strategica/creativa tua va etichettata \"SUGGESTION\"."
        )
    else:
        istruzioni_fonte = (
            "Non hai contenuto reale di nessun sito (non indicato, non raggiungibile, o generato via "
            "JavaScript e quindi illeggibile): dichiaralo nell'analisi invece di far finta di sapere cose "
            "che non sai. In questo caso ogni consiglio è per forza \"INFERENCE\" (dedotto da settore/"
            "prodotto/zona/target) o \"SUGGESTION\" — non puoi avere nessun \"FACT\" senza un sito letto davvero."
        )

    prompt = f"""Sei un consulente di marketing che aiuta una PMI a fare pubblicità su giornali e riviste italiane (carta e digitale editoriale), con un occhio di riguardo per aziende con budget limitato.

Dati azienda:
Nome: {payload.name}
Sito web: {payload.website or 'non indicato'}
Prodotto o servizio specifico: {payload.prodotto or 'non indicato, ragiona sul settore in generale'}
Zona geografica: {payload.zona_geografica or 'non indicata, presumi rilevanza nazionale'}
Target di clientela: {payload.target_cliente or 'non indicato'}
Settore: {payload.sector_label}
Tono di marca: {payload.tone or 'non specificato'}
Competitor noti: {', '.join(payload.competitors) if payload.competitors else 'nessuno indicato, usa benchmark di settore'}
Budget indicativo: € {payload.budget}
Obiettivo campagna: {payload.obiettivo or 'non indicato'}
{sito_block}{competitor_block}
{istruzioni_fonte}

REGOLA FONDAMENTALE: non inventare mai clienti, recensioni, fatturato, audience, diffusione, CPM, prezzi ufficiali, certificazioni, partnership, risultati di campagne, o dati demografici che non hai. Se qualcosa non è rilevabile dai dati disponibili, scrivi esplicitamente "non rilevato dal sito" o "non determinabile con i dati disponibili" invece di inventarlo.

Determina anche il raggio d'azione geografico REALE dell'azienda (serve al motore di allocazione budget, non solo come testo):
- "market_scope": "LOCAL" (una sola città/provincia), "REGIONAL" (una regione), "NATIONAL" (tutta Italia), o "UNKNOWN" se non determinabile
- "market_region": il nome della regione italiana se LOCAL o REGIONAL (es. "Lombardia"), altrimenti null
- "market_confidence": "HIGH" se il sito lo dichiara esplicitamente (es. zona di consegna/intervento, sede operativa, "serviamo la provincia di..."), "MEDIUM" se dedotto ragionevolmente ma non dichiarato esplicitamente, "LOW" se è solo un'ipotesi debole. Senza contenuto reale di un sito, o se il sito non dà indizi geografici, usa "UNKNOWN"/null/"LOW" — non indovinare.

Genera:
- "analisi_azienda": 2-3 frasi su cosa fa davvero questa azienda secondo quello che hai letto (o, se non hai contenuto reale, una frase che lo dichiara apertamente)
- "consigli_su_misura": 4-5 consigli CONCRETI per la campagna pubblicitaria, ciascuno un oggetto {{"testo":"...","tipo":"FACT|INFERENCE|SUGGESTION"}} secondo la regola sopra — non genericità valide per qualsiasi azienda del settore
- una "idea_sicura": basata su pattern collaudati del settore, basso rischio
- due "idee_audaci": meccanismi creativi presi in prestito da ALTRI settori, applicati in modo pertinente a questo brand

Tutte le idee devono essere realizzabili su carta stampata o adv editoriale digitale (niente tecnologie non disponibili su questi formati).

Rispondi SOLO con un oggetto JSON valido, nessun testo prima o dopo, in questo formato esatto:
{{"analisi_azienda":"...","market_scope":"LOCAL|REGIONAL|NATIONAL|UNKNOWN","market_region":"Lombardia","market_confidence":"HIGH|MEDIUM|LOW","consigli_su_misura":[{{"testo":"...","tipo":"FACT"}},{{"testo":"...","tipo":"INFERENCE"}}],"idea_sicura":{{"titolo":"...","meccanismo":"...","perche":"..."}},"idee_audaci":[{{"titolo":"...","meccanismo":"...","perche":"...","rischio":"...","novita":0}},{{"titolo":"...","meccanismo":"...","perche":"...","rischio":"...","novita":0}}]}}

"novita" è un numero da 0 a 100. Scrivi tutti i testi in italiano."""

    return prompt, ha_sito_reale


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

    prompt, ha_sito_reale = _build_analysis_prompt(payload)

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
                "max_tokens": 2500,
                # Il "thinking" esteso di alcuni modelli Claude consuma parte
                # del budget di max_tokens PRIMA di produrre il testo vero e
                # proprio: su un prompt come questo può da solo esaurire
                # max_tokens, troncando la risposta a zero testo (osservato
                # in test locale il 9/9/2026). Qui serve solo il JSON finale,
                # non un ragionamento visibile, quindi lo disabilitiamo:
                # risposta più affidabile e più economica.
                "thinking": {"type": "disabled"},
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=45,
        )
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="Il motore AI non ha risposto in tempo. Riprova.")
    except requests.exceptions.RequestException:
        raise HTTPException(status_code=502, detail="Impossibile contattare il motore AI. Riprova più tardi.")

    if resp.status_code != 200:
        # Mai esporre il corpo grezzo della risposta di Anthropic al client:
        # solo un messaggio generico, i dettagli restano nei log del server.
        raise HTTPException(status_code=502, detail="Il motore AI ha risposto con un errore. Riprova più tardi.")

    try:
        data = resp.json()
        text = "".join(block.get("text", "") for block in data.get("content", []))
        clean = re.sub(r"```json|```", "", text).strip()
        parsed = json.loads(clean)
    except Exception:
        raise HTTPException(status_code=502, detail="Risposta AI in un formato inatteso. Riprova.")

    if not isinstance(parsed.get("idea_sicura"), dict) or not isinstance(parsed.get("idee_audaci"), list):
        raise HTTPException(status_code=502, detail="Risposta AI incompleta. Riprova.")

    consigli_raw = parsed.get("consigli_su_misura")
    if not isinstance(consigli_raw, list):
        consigli_raw = []
    # Normalizza: se il modello risponde con semplici stringhe invece che
    # {testo, tipo}, non buttiamo via la risposta — etichetta di default
    # INFERENCE (mai "FACT" per qualcosa di cui non conosciamo la provenienza).
    consigli = []
    for c in consigli_raw:
        if isinstance(c, dict) and "testo" in c:
            tipo = c.get("tipo") if c.get("tipo") in ("FACT", "INFERENCE", "SUGGESTION") else "INFERENCE"
            consigli.append({"testo": c["testo"], "tipo": tipo})
        elif isinstance(c, str):
            consigli.append({"testo": c, "tipo": "INFERENCE"})

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

    return {
        "analisi_azienda": parsed.get("analisi_azienda", ""),
        "market_scope": market_scope,
        "market_region": market_region,
        "market_confidence": market_confidence,
        "consigli_su_misura": consigli,
        "idea_sicura": parsed["idea_sicura"],
        "idee_audaci": parsed["idee_audaci"],
        "sito_letto_davvero": ha_sito_reale,
    }
