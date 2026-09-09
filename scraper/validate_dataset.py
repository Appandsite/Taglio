"""
Taglio — validatore dataset aggregato
========================================

Confronta il nuovo aggregated.json (candidato, appena prodotto da
aggregator.py in questo run automatico) con quello attualmente pubblicato,
e decide se è abbastanza sano da sostituirlo. Se la scansione è fallita
quasi del tutto o il numero di testate con segnali crolla in modo anomalo
rispetto a prima, il dataset precedente resta quello pubblicato — mai un
aggiornamento peggiore di zero dati sostituisce dati reali già raccolti
(vedi istruzioni del 9/9/2026, punto 3 — "aggiornamento automatico delle
testate").

Scrive sempre dataset_meta.json con l'esito di QUESTO tentativo (anche se
fallito, per trasparenza), ma aggiorna "ultimo_aggiornamento" (la data
mostrata in UI) solo quando il nuovo dataset viene davvero pubblicato.

Uso:
    python validate_dataset.py --previous aggregated.json \
        --candidate aggregated.new.json --run-summary-dir out \
        --promote-to aggregated.json --meta-output dataset_meta.json

Esce con codice 0 se il dataset è stato pubblicato, 1 se è stato scartato
(il workflow CI usa questo codice per decidere se fare il commit).
"""

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

# Sotto questa frazione del dataset precedente, un calo del numero di
# testate con segnali validi è considerato anomalo (es. blocco anti-bot
# diffuso, timeout di rete generalizzato) piuttosto che normale variazione
# settimanale — meglio tenere il dataset precedente che uno dimezzato.
SOGLIA_CALO_ANOMALO = 0.5

# Se più di questa frazione delle testate tentate fallisce, il run è
# considerato inaffidabile nel suo complesso (es. IP bloccato, Chromium
# non installato correttamente) anche se qualche testata isolata è passata.
SOGLIA_ERRORE_DIFFUSO = 0.9


def load_json(path: Path):
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def latest_run_summary(run_summary_dir: Path):
    files = sorted(run_summary_dir.glob("run_summary_*.json"))
    if not files:
        return None
    return load_json(files[-1])


def valuta(previous: list | None, candidate: list | None, run_summary: dict | None) -> tuple[bool, list[str]]:
    """Ritorna (pubblicabile, motivi_di_scarto)."""
    motivi = []

    if candidate is None:
        return False, ["aggregated.new.json mancante o non leggibile: aggregator.py non ha prodotto un output valido"]

    if len(candidate) == 0:
        motivi.append("il nuovo dataset non contiene nessuna testata con segnali validi")

    previous_count = len(previous) if previous else 0
    candidate_count = len(candidate)
    if previous_count > 0 and candidate_count < previous_count * SOGLIA_CALO_ANOMALO:
        motivi.append(
            f"calo anomalo delle testate con segnali: {candidate_count} contro {previous_count} "
            f"del dataset precedente (soglia minima accettata: {round(previous_count * SOGLIA_CALO_ANOMALO)})"
        )

    if run_summary:
        tentate = run_summary.get("testate_tentate", 0)
        errori = len(run_summary.get("testate_con_errore", []))
        if tentate > 0:
            raggiunte = tentate - errori
            if raggiunte == 0:
                motivi.append("tutte le testate tentate in questo run sono fallite")
            elif errori / tentate > SOGLIA_ERRORE_DIFFUSO:
                motivi.append(
                    f"più del {int(SOGLIA_ERRORE_DIFFUSO * 100)}% delle testate tentate è fallito "
                    f"({errori}/{tentate})"
                )

    return (len(motivi) == 0), motivi


def main():
    parser = argparse.ArgumentParser(description="Valida il dataset aggregato prima di pubblicarlo")
    parser.add_argument("--previous", default="aggregated.json")
    parser.add_argument("--candidate", default="aggregated.new.json")
    parser.add_argument("--run-summary-dir", default="out")
    parser.add_argument("--promote-to", default="aggregated.json")
    parser.add_argument("--meta-output", default="dataset_meta.json")
    args = parser.parse_args()

    previous_path = Path(args.previous)
    candidate_path = Path(args.candidate)
    run_summary_dir = Path(args.run_summary_dir)
    meta_path = Path(args.meta_output)

    previous = load_json(previous_path)
    candidate = load_json(candidate_path)
    run_summary = latest_run_summary(run_summary_dir)

    pubblicabile, motivi = valuta(previous, candidate, run_summary)

    now_utc = datetime.now(timezone.utc).isoformat()
    meta_precedente = load_json(meta_path) or {}

    meta = dict(meta_precedente)
    meta["ultimo_run_timestamp_utc"] = now_utc
    meta["ultimo_run_esito"] = "OK" if pubblicabile else "FALLITO"
    meta["ultimo_run_motivi_scarto"] = motivi
    if run_summary:
        meta["ultimo_run_testate_configurate"] = run_summary.get("testate_configurate")
        meta["ultimo_run_testate_tentate"] = run_summary.get("testate_tentate")
        meta["ultimo_run_testate_con_errore"] = run_summary.get("testate_con_errore", [])
        meta["ultimo_run_testate_con_segnali_grezzi"] = run_summary.get("testate_con_segnali", [])
    meta["ultimo_run_testate_nel_dataset_candidato"] = len(candidate) if candidate is not None else 0

    if pubblicabile:
        candidate_path.replace(Path(args.promote_to))
        meta["ultimo_aggiornamento"] = date.today().isoformat()
        meta["ultimo_aggiornamento_timestamp_utc"] = now_utc
        print(f"Dataset pubblicato: {len(candidate)} testate con segnali (prima: {len(previous) if previous else 0}).")
    else:
        print("Dataset NON pubblicato — il precedente resta quello valido. Motivi:")
        for m in motivi:
            print(f"  - {m}")

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    sys.exit(0 if pubblicabile else 1)


if __name__ == "__main__":
    main()
