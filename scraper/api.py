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
wizard, per dare all'AI un contesto reale invece di solo nome/URL.

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
"""

import ipaddress
import json
import os
import re
import socket
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
import stripe
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

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

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://oxuirmbgbwnegnqxfdjx.supabase.co")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
SITE_URL = os.environ.get("SITE_URL", "https://appandsite.github.io/Taglio/")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


@app.get("/api/allocation")
def get_allocation(settore: Optional[str] = None):
    if not AGGREGATED_FILE.exists():
        raise HTTPException(
            status_code=404,
            detail="aggregated.json non trovato. Esegui prima scraper.py e poi aggregator.py.",
        )
    with open(AGGREGATED_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    if settore:
        allowed = categorie_rilevanti(settore)
        data = [d for d in data if set(d.get("categorie", ["generalista"])) & allowed]

    return data


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
    if len(text) < 200:
        return {
            "ok": False,
            "error": "Il sito sembra generato via JavaScript: il contenuto non è leggibile senza eseguirlo in un browser",
        }

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

    return {"ok": True, "title": title, "testo": text[:6000]}
