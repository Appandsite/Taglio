"""
Taglio — scraper ad slot testate
=================================

Cosa fa:
- Visita le pagine configurate in config.yaml con un browser headless
- Intercetta le richieste di rete verso domini pubblicitari noti (ad server,
  network programmatici) mentre la pagina carica
- Registra dimensioni e posizione degli slot pubblicitari trovati
- Cerca, tra i link della pagina stessa (di solito nel footer), quello alla
  sezione commerciale/pubblicitaria del sito — se lo trova lo riporta come
  link cliccabile, senza mai indovinare un numero o una mail
- NON legge, salva o analizza il contenuto editoriale della pagina

Cosa NON fa (limiti da tenere presenti):
- Non stima i prezzi pagati (mai pubblici)
- Non misura le performance reali (CTR, conversioni)
- Non aggira paywall né estrae contenuti protetti da copyright
- Va usato rispettando robots.txt e i termini di servizio di ogni testata

Uso:
    python scraper.py --config config.yaml --output out/

Setup:
    pip install -r requirements.txt
    playwright install chromium
"""

import argparse
import asyncio
import json
import urllib.robotparser as robotparser
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml
from playwright.async_api import async_playwright, Page, Request

from taxonomy import SETTORE_TAGS, categorie_rilevanti

# Domini di ad server / network programmatici noti.
# Estendi questa lista man mano che ne incontri di nuovi durante i test.
AD_DOMAIN_PATTERNS = [
    "doubleclick.net",
    "googlesyndication.com",
    "google.com/pagead",
    "adnxs.com",
    "criteo.com",
    "criteo.net",
    "taboola.com",
    "outbrain.com",
    "smartadserver.com",
    "adform.net",
    "rubiconproject.com",
    "pubmatic.com",
    "casalemedia.com",
    "indexexchange.com",
    "openx.net",
    "yieldlab.net",
    "improvedigital.com",
    "adition.com",
    "amazon-adsystem.com",
]


def filter_by_settore(testate: list[dict], settore: str | None) -> list[dict]:
    """Filtra le testate in base al settore, usando il campo 'categorie' del config.
    Senza --settore, restituisce tutte le testate (comportamento invariato)."""
    if not settore:
        return testate
    allowed = categorie_rilevanti(settore)
    filtered = [t for t in testate if set(t.get("categorie", ["generalista"])) & allowed]
    print(f"Filtro per settore '{settore}': {len(filtered)} di {len(testate)} testate selezionate")
    return filtered


@dataclass
class AdSlotObservation:
    testata: str
    pagina: str
    dominio_ad: str
    resource_type: str
    larghezza: int | None
    altezza: int | None
    pos_x: float | None
    pos_y: float | None
    timestamp_utc: str
    # Link alla sezione pubblicità/commerciale trovato sul sito stesso della
    # testata (es. "Pubblicità" nel footer), se presente. Ripetuto identico
    # su ogni osservazione della stessa testata/scan per restare nello stesso
    # schema piatto — aggregator.py lo estrae una volta per testata. Mai
    # inventato: None se lo scraper non trova un link esplicito.
    pagina_pubblicita: str | None = None


def is_ad_request(url: str) -> str | None:
    """Ritorna il dominio ad matchato, o None se la request non è pubblicitaria."""
    for pattern in AD_DOMAIN_PATTERNS:
        if pattern in url:
            return pattern
    return None


# Selettori dei più diffusi CMP (Consent Management Platform) usati dagli
# editori italiani/EU. Prima scelta perché precisi; il fallback testuale
# sotto copre i CMP custom o non riconosciuti.
COOKIE_BANNER_SELECTORS = [
    "#onetrust-accept-btn-handler",           # OneTrust
    ".iubenda-cs-accept-btn",                 # Iubenda
    "#iubFooterBtn",                          # Iubenda (variante)
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",  # Cookiebot
    "#CybotCookiebotDialogBodyButtonAccept",  # Cookiebot (variante)
    "#didomi-notice-agree-button",            # Didomi
    ".qc-cmp2-summary-buttons button[mode='primary']",  # Quantcast Choice / TCF
    "button[title='Accetta tutto']",
    "button[title='Accetta e chiudi']",
    "#pt-accept-all",                         # Piano/Poool paywall CMP
]

# Testi comuni sui pulsanti di accettazione (case-insensitive, match esatto
# o come sottostringa del testo del bottone).
COOKIE_BANNER_TEXTS = [
    "accetta tutto",
    "accetta e chiudi",
    "accetta i cookie",
    "accetto tutti",
    "acconsento",
    "accetta",
    "consento",
    "accept all",
    "i agree",
    "agree",
]


async def dismiss_cookie_banner(page: Page, click_timeout_ms: int = 2000) -> str | None:
    """Prova a chiudere il banner di consenso cookie/GDPR se presente.
    Ritorna una descrizione di cosa ha cliccato, o None se non ha trovato nulla
    (non è un errore: molte testate non mostrano banner, es. se già accettato).

    Controlla prima la presenza dell'elemento (count(), istantaneo) e clicca
    solo se esiste davvero: a banner assente, i selettori sbagliati non hanno
    motivo di aspettare — a differenza di un click diretto con timeout lungo,
    che se ripetuto su 10 selettori per decine di testate rallenterebbe
    parecchio l'intera scansione."""
    for selector in COOKIE_BANNER_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            await locator.click(timeout=click_timeout_ms)
            return f"selettore {selector}"
        except Exception:
            continue

    # Fallback: cerca un bottone/link visibile il cui testo corrisponde a una
    # delle frasi comuni di accettazione, anche dentro iframe di terze parti
    # (molti CMP montano il banner in un iframe separato).
    for frame in page.frames:
        for text in COOKIE_BANNER_TEXTS:
            try:
                locator = frame.get_by_role("button", name=text, exact=False).first
                if await locator.count() == 0:
                    continue
                await locator.click(timeout=click_timeout_ms)
                return f"testo '{text}'"
            except Exception:
                continue
    return None


# Testi comuni per il link, di solito nel footer, alla sezione
# commerciale/pubblicitaria del sito (spesso porta alla concessionaria che
# vende gli spazi). Cerchiamo solo questo — mai un numero o una mail
# indovinati, solo un link che la testata stessa pubblica.
AD_CONTACT_LINK_KEYWORDS = [
    "pubblicità", "pubblicita", "advertising", "info pubblicitarie",
    "media kit", "mediakit", "concessionaria", "advertise with us",
    "spazi pubblicitari",
]


async def find_advertising_contact_url(page: Page) -> str | None:
    """Cerca un link della pagina il cui testo suggerisce la sezione
    commerciale/pubblicitaria del sito. Ritorna None se non lo trova — non
    fabbrica mai un contatto plausibile ma non verificato."""
    try:
        links = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({href: e.href, text: (e.textContent||'').trim().toLowerCase()}))",
        )
    except Exception:
        return None
    for link in links:
        text = link.get("text", "")
        href = link.get("href", "")
        if href.startswith("http") and any(kw in text for kw in AD_CONTACT_LINK_KEYWORDS):
            return href
    return None


def check_robots_allowed(url: str, user_agent: str = "*") -> bool:
    """Controlla robots.txt prima di procedere. Se non è raggiungibile, procede con cautela."""
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = robotparser.RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.read()
        return rp.can_fetch(user_agent, url)
    except Exception:
        # Se robots.txt non è leggibile, non blocchiamo ma segnaliamo nel log.
        print(f"  [attenzione] impossibile leggere {robots_url}, procedo con cautela")
        return True


async def scan_testata(browser, testata: dict, timeout_ms: int = 30000) -> list[AdSlotObservation]:
    """Visita una singola testata e restituisce le osservazioni di ad slot trovate."""
    name = testata["name"]
    url = testata["url"]
    observations: list[AdSlotObservation] = []

    if not check_robots_allowed(url):
        print(f"  [saltata] {name}: robots.txt nega la scansione di questo url")
        return observations

    context = await browser.new_context(
        viewport={"width": 1366, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 TaglioBot/0.1"
        ),
    )
    page: Page = await context.new_page()

    seen_domains: set[str] = set()

    async def on_request(request: Request):
        ad_domain = is_ad_request(request.url)
        if ad_domain:
            seen_domains.add(ad_domain)

    page.on("request", on_request)

    try:
        observations = await _scan_page(page, name, url, seen_domains, timeout_ms)
    finally:
        # Garantito anche se qualcosa sopra solleva un'eccezione non prevista:
        # un context Playwright non chiuso è una risorsa (processo/memoria) che
        # resta viva finché non chiudi il browser, e su una scansione di
        # decine di testate si accumula in fretta.
        await context.close()
    return observations


async def _scan_page(page: Page, name: str, url: str, seen_domains: set[str], timeout_ms: int) -> list[AdSlotObservation]:
    observations: list[AdSlotObservation] = []

    try:
        await page.goto(url, wait_until="load", timeout=timeout_ms)
    except Exception as e:
        print(f"  [errore] {name}: {e}")
        return observations

    # networkidle è preferibile (dà più tempo agli slot pubblicitari di
    # comparire) ma alcune testate non lo raggiungono mai: refresh continui di
    # ad/analytics in background tengono la rete "occupata" all'infinito. In
    # quel caso continuiamo comunque con quanto già caricato invece di perdere
    # l'intera testata per un timeout.
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass

    dismissed = await dismiss_cookie_banner(page)
    if dismissed:
        print(f"  [cookie] {name}: banner di consenso chiuso ({dismissed})")
        # Alcuni CMP (es. OneTrust su certe testate) ricaricano la pagina dopo
        # il consenso: aspettiamo che il nuovo "load" sia pronto invece di una
        # semplice pausa fissa, altrimenti lo scroll successivo può cadere su
        # un frame appena distrutto dalla navigazione ("execution context
        # destroyed").
        try:
            await page.wait_for_load_state("load", timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(1000)

    # Il link "Pubblicità" è quasi sempre statico nel footer, quindi
    # cercarlo qui (pagina già caricata, banner già chiuso) è sufficiente
    # senza aspettare lo scroll. Fallisce in silenzio: non deve mai
    # interrompere la scansione degli ad slot per questo.
    try:
        pagina_pubblicita = await find_advertising_contact_url(page)
    except Exception:
        pagina_pubblicita = None

    # Molti banner sono lazy-loaded: scrolliamo per farli comparire. Questa
    # fase può ancora incontrare una navigazione imprevista (redirect, reload
    # ritardato) — non deve far perdere l'intero batch, solo questa testata.
    try:
        await autoscroll(page)
        await page.wait_for_timeout(2000)

        # Per ogni iframe pubblicitario visibile, prendiamo dimensioni e posizione.
        frames = page.frames
        for frame in frames:
            frame_url = frame.url
            ad_domain = is_ad_request(frame_url)
            if not ad_domain:
                continue
            try:
                frame_element = await frame.frame_element()
                box = await frame_element.bounding_box()
            except Exception:
                box = None

            observations.append(
                AdSlotObservation(
                    testata=name,
                    pagina=url,
                    dominio_ad=ad_domain,
                    resource_type="iframe",
                    larghezza=int(box["width"]) if box else None,
                    altezza=int(box["height"]) if box else None,
                    pos_x=round(box["x"], 1) if box else None,
                    pos_y=round(box["y"], 1) if box else None,
                    timestamp_utc=datetime.now(timezone.utc).isoformat(),
                    pagina_pubblicita=pagina_pubblicita,
                )
            )
    except Exception as e:
        print(f"  [attenzione] {name}: interrotto durante scroll/lettura frame ({e}), uso solo i segnali di rete già visti")

    # Domini ad rilevati solo via network request, senza iframe identificabile
    # (tracking pixel, chiamate header bidding, ecc.) — utile come segnale di presenza.
    frame_domains = {o.dominio_ad for o in observations}
    for domain in seen_domains - frame_domains:
        observations.append(
            AdSlotObservation(
                testata=name,
                pagina=url,
                dominio_ad=domain,
                resource_type="network_only",
                larghezza=None,
                altezza=None,
                pos_x=None,
                pos_y=None,
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                pagina_pubblicita=pagina_pubblicita,
            )
        )

    return observations


async def autoscroll(page: Page, step: int = 600, pause_ms: int = 350, max_steps: int = 12):
    """Scrolla la pagina gradualmente per attivare il caricamento lazy dei banner."""
    for _ in range(max_steps):
        await page.evaluate(f"window.scrollBy(0, {step})")
        await page.wait_for_timeout(pause_ms)


async def run(config_path: str, output_dir: str, delay_seconds: float, headless: bool, concurrency: int, settore: str | None):
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    testate = config.get("testate", [])
    if not testate:
        print("Nessuna testata configurata in config.yaml")
        return

    testate = filter_by_settore(testate, settore)
    if not testate:
        print("Nessuna testata corrisponde al settore indicato.")
        return

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    all_observations: list[AdSlotObservation] = []
    semaphore = asyncio.Semaphore(concurrency)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)

        async def worker(index: int, testata: dict):
            async with semaphore:
                print(f"[{index+1}/{len(testate)}] Scansione: {testata['name']}")
                try:
                    obs = await scan_testata(browser, testata)
                except Exception as e:
                    # Un errore imprevisto su una singola testata non deve far perdere
                    # i risultati già raccolti dalle altre in questo stesso batch.
                    print(f"  [errore] {testata['name']}: fallita ({e})")
                    return []
                print(f"  {testata['name']}: trovati {len(obs)} segnali pubblicitari")
                return obs

        tasks = [worker(i, t) for i, t in enumerate(testate)]
        results = await asyncio.gather(*tasks)
        for obs in results:
            all_observations.extend(obs)

        await browser.close()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_file = Path(output_dir) / f"scan_{timestamp}.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump([asdict(o) for o in all_observations], f, ensure_ascii=False, indent=2)

    print(f"\nFatto. {len(all_observations)} osservazioni da {len(testate)} testate salvate in {output_file}")


def main():
    parser = argparse.ArgumentParser(description="Taglio — scraper ad slot testate")
    parser.add_argument("--config", default="config.yaml", help="Percorso file di configurazione")
    parser.add_argument("--output", default="out", help="Cartella di output per i risultati JSON")
    parser.add_argument("--delay", type=float, default=3.0, help="Non più usato con la concorrenza, mantenuto per compatibilità")
    parser.add_argument("--headed", action="store_true", help="Esegui con browser visibile (utile per debug)")
    parser.add_argument("--concurrency", type=int, default=4, help="Quante testate scansionare in parallelo (default 4)")
    parser.add_argument("--settore", choices=list(SETTORE_TAGS.keys()), default=None,
                         help="Filtra le testate per settore azienda, scansionando solo quelle pertinenti")
    args = parser.parse_args()

    asyncio.run(run(args.config, args.output, args.delay, headless=not args.headed,
                     concurrency=args.concurrency, settore=args.settore))


if __name__ == "__main__":
    main()
