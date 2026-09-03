"""
Taglio — backend API
======================

Espone i dati aggregati dello scraper al sito via HTTP, così il frontend
può leggerli con una semplice fetch() invece di dati finti.

Uso:
    uvicorn api:app --reload --port 8000

Poi apri http://localhost:8000/api/allocation nel browser per verificare
che risponda con il JSON prima di collegarlo al sito.
"""

import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from taxonomy import categorie_rilevanti

app = FastAPI(title="Taglio API")

# In sviluppo va bene aperto a tutti ("*"). In produzione limita
# allow_origins al dominio reale dove pubblichi il sito.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

AGGREGATED_FILE = Path("aggregated.json")


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
