"""
Taglio — backend API
======================

Espone i dati aggregati dello scraper al sito via HTTP, così il frontend
può leggerli con una semplice fetch() invece di dati finti. Gestisce anche
l'abbonamento a pagamento (Stripe) e lo stato "abbonato" degli utenti
(Supabase): dopo le ricerche gratuite, il sito chiama /api/create-checkout-
session per far pagare l'utente, e Stripe notifica il pagamento riuscito a
/api/stripe-webhook, che aggiorna il profilo su Supabase.

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

import json
import os
from pathlib import Path
from typing import Optional

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
SITE_URL = os.environ.get("SITE_URL", "https://tmysceppa-ctrl.github.io/Taglio/")

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
