"""
Taglio — tassonomia condivisa settore -> categorie testate.

Usata da scraper.py (per filtrare quali testate scansionare), aggregator.py
(per allegare le categorie ai dati aggregati) e api.py (per filtrare
l'allocazione richiesta dal sito). Le chiavi settore corrispondono a quelle
usate nel menu "Settore" del sito (site/taglio-demo.html).
"""

SETTORE_TAGS = {
    "automotive": ["generalista", "automotive", "sport"],
    "moda":       ["generalista", "moda", "lifestyle", "gossip"],
    "food":       ["generalista", "food", "lifestyle"],
    "tech":       ["generalista", "tech"],
    "finanza":    ["generalista", "finanza"],
    "retail":     ["generalista", "gossip", "lifestyle"],
    "altro":      ["generalista"],
}


def categorie_rilevanti(settore: str | None) -> set[str]:
    """Ritorna l'insieme di categorie rilevanti per un settore. None = nessun filtro."""
    if not settore:
        return set()
    return set(SETTORE_TAGS.get(settore, ["generalista"]))
