"""
Taglio — aggregatore risultati scraper
========================================

Legge tutti gli scan_*.json prodotti da scraper.py in una cartella e li
riassume in un unico file aggregated.json, con una riga per testata:
quanti segnali pubblicitari sono stati osservati, il formato più comune (con
una categoria di prezzo derivata, per stimare il costo anche sui dati
reali), una quota % puramente proporzionale alla frequenza osservata, uno
storico basato sui timestamp di ogni osservazione — per quanti giorni
consecutivi (fino all'ultima scansione disponibile) un dato dominio ad
resta presente sulla testata, come proxy di "campagna in corso" — un
suggerimento di posizionamento (sopra/sotto la piega) basato su quale zona
della pagina ha meno reti pubblicitarie già attive, e il link alla sezione
pubblicità del sito quando lo scraper l'ha trovato (mai un contatto
inventato).

Questa è una metrica MVP: presenza osservata, non prestazione reale.
Man mano che accumuli più scansioni nel tempo (via cron, vedi README), sia
la quota che lo storico migliorano perché si stabilizzano sulla frequenza
media invece che su una singola sessione.

Uso:
    python aggregator.py --input out/ --output aggregated.json
"""

import argparse
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml


def load_all_scans(input_dir: str) -> list[dict]:
    observations = []
    for path in sorted(Path(input_dir).glob("scan_*.json")):
        with open(path, "r", encoding="utf-8") as f:
            observations.extend(json.load(f))
    return observations


def load_categorie_map(config_path: str) -> dict[str, list[str]]:
    """Legge config.yaml e ritorna {nome_testata: [categorie]}. Vuoto se il file manca."""
    path = Path(config_path)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    return {t["name"]: t.get("categorie", ["generalista"]) for t in config.get("testate", [])}


def parse_date(timestamp_utc: str) -> date:
    return datetime.fromisoformat(timestamp_utc).date()


def longest_streak_ending_at(days: set[date], reference: date) -> int:
    """Conta i giorni consecutivi in 'days' andando a ritroso da 'reference'.
    Se la testata non è stata scansionata proprio in 'reference' (es. una
    testata saltata in quel run), lo streak parte da 0 — non è "campagna
    ancora attiva" se non l'abbiamo osservata nell'ultima scansione utile."""
    streak = 0
    day = reference
    while day in days:
        streak += 1
        day -= timedelta(days=1)
    return streak


def most_common_format(sizes: list[tuple[int, int]]) -> str:
    # Scarta dimensioni degeneri (1x1, 0x0...): sono quasi sempre pixel di
    # tracking nascosti, non spazi pubblicitari reali che un'azienda possa
    # acquistare — riportarli come "formato" della testata sarebbe fuorviante.
    valid = [s for s in sizes if s[0] and s[1] and s[0] > 5 and s[1] > 5]
    if not valid:
        return "Formato vario"
    counter = Counter(valid)
    (w, h), _ = counter.most_common(1)[0]
    return f"{w}x{h}"


def formato_categoria(w: int | None, h: int | None) -> str:
    """Classifica una dimensione di slot pubblicitario in una delle categorie
    di prezzo usate dal sito (site/taglio-demo.html: FORMATO_BASE). Lo
    scraper vede solo formati digitali (mai 'pagina intera' o 'doppia
    pagina', concetti di stampa che qui non hanno senso), quindi la scelta è
    tra banner classico e native/rettangolo più integrato nel contenuto."""
    if not w or not h:
        return "default"
    # Leaderboard/billboard (es. 728x90, 970x250) e skyscraper (es. 160x600)
    # sono banner classici anche quando non bassissimi in pixel: quello che
    # li rende "banner" è la forma molto allungata, non solo l'altezza.
    if h <= 100 or w / h >= 3 or (w <= 200 and h >= 400):
        return "banner"
    if w >= 250 and h >= 200:
        return "native"
    return "default"


# Sotto questa altezza (px) uno slot è visibile senza scroll, nello stesso
# viewport usato da scraper.py per aprire le pagine — quindi "sopra la
# piega" in questo contesto, non un valore assoluto universale.
FOLD_THRESHOLD_PX = 900


def posizionamento_consigliato(obs_list: list[dict]) -> dict | None:
    """Suggerisce in quale zona della pagina (sopra o sotto la piega) un
    nuovo inserzionista trova meno concorrenza già presente, usando la
    posizione degli slot iframe effettivamente osservati.

    Attenzione ai limiti: questo è un segnale di densità di reti
    pubblicitarie per zona, non l'identificazione di dove specifici
    competitor sono posizionati — lo scraper non legge il contenuto/brand
    delle creatività (vedi docstring di scraper.py), solo quali domini ad
    server sono attivi. È comunque un'indicazione utile di "quanto è
    conteso" uno spazio, non di chi lo occupa."""
    by_zona: dict[str, set[str]] = defaultdict(set)
    for o in obs_list:
        if o.get("resource_type") != "iframe" or o.get("pos_y") is None:
            continue
        zona = "sopra la piega" if o["pos_y"] < FOLD_THRESHOLD_PX else "sotto la piega"
        by_zona[zona].add(o["dominio_ad"])

    if not by_zona:
        return None

    zona_scelta, domini = min(by_zona.items(), key=lambda kv: len(kv[1]))
    altre_zone = {z: len(d) for z, d in by_zona.items() if z != zona_scelta}

    return {
        "zona": zona_scelta,
        "inserzionisti_rilevati_qui": len(domini),
        "inserzionisti_rilevati_altrove": altre_zone,
    }


def aggregate(observations: list[dict], categorie_map: dict[str, list[str]]) -> list[dict]:
    by_testata: dict[str, list[dict]] = defaultdict(list)
    for obs in observations:
        by_testata[obs["testata"]].append(obs)

    if not by_testata:
        return []

    # Data di riferimento per lo streak: l'ultimo giorno in cui è stata fatta
    # una scansione, su tutto il dataset. Ancora comune a tutte le testate,
    # così gli streak restano confrontabili tra loro.
    all_dates = {parse_date(o["timestamp_utc"]) for o in observations}
    reference_date = max(all_dates)

    total_signals = sum(len(v) for v in by_testata.values())
    results = []
    for testata, obs_list in by_testata.items():
        sizes = [(o.get("larghezza"), o.get("altezza")) for o in obs_list]
        distinct_domains = {o["dominio_ad"] for o in obs_list}
        quota = round(100 * len(obs_list) / total_signals, 1) if total_signals else 0

        formato_principale = most_common_format(sizes)
        w, h = (None, None)
        if formato_principale != "Formato vario":
            w, h = (int(x) for x in formato_principale.split("x"))

        giorni_osservati = sorted({parse_date(o["timestamp_utc"]) for o in obs_list})

        # Per ogni dominio ad, i giorni distinti in cui è comparso su questa
        # testata: la base per stimare per quanto tempo resta "acceso" uno slot.
        days_by_domain: dict[str, set[date]] = defaultdict(set)
        for o in obs_list:
            days_by_domain[o["dominio_ad"]].add(parse_date(o["timestamp_utc"]))

        streaks = {
            domain: longest_streak_ending_at(days, reference_date)
            for domain, days in days_by_domain.items()
        }
        campagna_dominio, campagna_giorni = max(streaks.items(), key=lambda kv: kv[1], default=(None, 0))

        # Link alla sezione pubblicità del sito, se lo scraper l'ha trovato
        # (mai un contatto inventato — vedi scraper.py). Prende il più
        # recente tra le osservazioni, nel caso sia cambiato tra scansioni.
        contatti = [o for o in obs_list if o.get("pagina_pubblicita")]
        contatto_pubblicitario_url = max(contatti, key=lambda o: o["timestamp_utc"])["pagina_pubblicita"] if contatti else None

        results.append({
            "nome": testata,
            "formato": formato_principale,
            "formato_categoria": formato_categoria(w, h),
            "quota": quota,
            "segnali_osservati": len(obs_list),
            "domini_ad_distinti": sorted(distinct_domains),
            "categorie": categorie_map.get(testata, ["generalista"]),
            "prima_osservazione": giorni_osservati[0].isoformat(),
            "ultima_osservazione": giorni_osservati[-1].isoformat(),
            "giorni_scansionati": len(giorni_osservati),
            "campagna_probabile": {
                "dominio": campagna_dominio,
                "giorni_consecutivi": campagna_giorni,
            } if campagna_dominio else None,
            "posizionamento_consigliato": posizionamento_consigliato(obs_list),
            "contatto_pubblicitario_url": contatto_pubblicitario_url,
        })

    # Normalizza le quote così che sommino a 100 anche dopo l'arrotondamento
    results.sort(key=lambda r: r["quota"], reverse=True)
    return results


def main():
    parser = argparse.ArgumentParser(description="Aggrega gli output dello scraper Taglio")
    parser.add_argument("--input", default="out", help="Cartella con i file scan_*.json")
    parser.add_argument("--output", default="aggregated.json", help="File di output")
    parser.add_argument("--config", default="config.yaml", help="Config con le categorie per testata")
    args = parser.parse_args()

    observations = load_all_scans(args.input)
    if not observations:
        print(f"Nessuna osservazione trovata in {args.input}. Esegui prima scraper.py.")
        return

    categorie_map = load_categorie_map(args.config)
    results = aggregate(observations, categorie_map)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"Aggregati {len(observations)} segnali da {len(results)} testate in {args.output}")


if __name__ == "__main__":
    main()
