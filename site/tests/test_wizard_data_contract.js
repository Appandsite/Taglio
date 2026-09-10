// Test di regressione per il bug pubblico del 10/9/2026: un utente reale
// che inseriva azienda (URL), competitor (URL, mai "Aggiunto" col click),
// budget, periodo, e lasciava zona/settore/cliente vuoti, otteneva:
//   - "Input should be a valid string" (422 di Pydantic: tone inviato null,
//     perché state.tone partiva null e nessun campo lo riportava a stringa)
//   - "Quello che abbiamo capito di la tua azienda" (nome mai risolto dal
//     sito quando l'utente non scrive un "Nome azienda" separato dall'URL)
//   - "Non hai indicato un competitor" nonostante il competitor scritto
//     (mai aggiunto a state.competitors senza il click su "Aggiungi")
//   - consigli generici ("valuta di alzare il budget", ecc.) anche a
//     fallimento AI totale
//
// Esegui con: node site/tests/test_wizard_data_contract.js
// Esce con codice 1 se un controllo fallisce.
const path = require('path');
const { loadPage } = require('./dom_harness.js');
const HTML_PATH = path.join(__dirname, '..', 'taglio-demo.html');

let esitiFalliti = 0;
function check(descrizione, condizione) {
  const ok = !!condizione;
  console.log((ok ? 'PASS' : 'FAIL') + ' — ' + descrizione);
  if (!ok) esitiFalliti++;
}

function creaFetchMock(scenario, catturaPayload) {
  return async function fetchMock(url, opts) {
    if (url.includes('/api/fetch-site-summary')) {
      if (url.includes('nmedilizia')) {
        return { ok: true, json: async () => ({ ok: true, title: 'NM Edilizia | Impresa di ristrutturazioni a Milano e in Lombardia', testo: 'x'.repeat(200) }) };
      }
      if (url.includes('ciessecostruzionimilano')) {
        return { ok: true, json: async () => ({ ok: true, title: 'Ciesse Costruzioni Milano | Ristrutturazioni Appartamenti e Bagni', testo: 'y'.repeat(200) }) };
      }
      return { ok: true, json: async () => ({ ok: false, error: 'n/a' }) };
    }
    if (url.includes('/api/generate-analysis')) {
      catturaPayload.valore = JSON.parse(opts.body);
      if (scenario === '422_tone_null') {
        // Riproduce esattamente la risposta di FastAPI/Pydantic osservata
        // in produzione quando un campo dichiarato "str" riceve null.
        return {
          ok: false, status: 422,
          json: async () => ({ detail: [{ loc: ['body', 'tone'], msg: 'Input should be a valid string', type: 'string_type' }] }),
        };
      }
      return {
        ok: true, status: 200,
        json: async () => ({
          analisi_azienda: 'ok', market_scope: 'REGIONAL', market_region: 'Lombardia', market_city: null,
          market_confidence: 'HIGH', business_model: 'B2C',
          azienda: {
            attivita: { testo: 'ristrutturazioni', stato: 'RILEVATO' },
            subsector: { testo: 'Ristrutturazioni residenziali chiavi in mano', stato: 'RILEVATO' },
          },
          main_products_services: [], customer_intents: [],
          competitor_confronto: { disponibile: true, tu_comunichi_meglio: ['x'], competitor_comunica_meglio: [], opportunita: [] },
          media_strategy: [], opportunita_migliori: [{ nome: 'Il Giorno', verdetto: 'CONTACT', domande_concessionaria: ['Domanda?'] }],
          media_da_approfondire: [], dove_non_investirei: [], canali_alternativi: [],
          messaggio_pubblicitario: { cta: 'Chiama ora' },
          creativita: { consigliata: { titolo: 'Titolo creativo' } },
          is_plus: false, sito_letto_davvero: true,
        }),
      };
    }
    throw new Error('URL non atteso nel mock: ' + url);
  };
}

// Input del wizard che riproducono esattamente il test pubblico:
// azienda solo URL, sector/zona/cliente vuoti, competitor scritto ma MAI
// aggiunto con il pulsante "Aggiungi", tono di voce mai toccato (default).
const CAMPI_WIZARD = {
  companyName: '', website: 'https://www.nmedilizia.it/', prodotto: '',
  zonaGeografica: '', targetCliente: '', sector: '', period: '1mese', competitorInput: '',
};

async function eseguiScenario(scenario) {
  const catturaPayload = {};
  const { context, getEl } = loadPage(HTML_PATH, CAMPI_WIZARD);
  getEl('competitorInput').value = 'https://www.ciessecostruzionimilano.it/'; // scritto, MAI aggiunto col click
  context.fetch = creaFetchMock(scenario, catturaPayload);
  context.__setTestProfile({ abbonato: true, ricerche_usate: 0 });
  await context.runAnalysis();
  return { getEl, payload: catturaPayload.valore };
}

async function main() {
  console.log('=== Scenario: AI risponde 422 (payload malformato, come nel bug pubblico) ===');
  const s1 = await eseguiScenario('422_tone_null');
  check('tone inviato come stringa, mai null', typeof s1.payload.tone === 'string');
  check('sectorKey/zonaGeografica/targetCliente/customerType sono stringhe, mai null',
    ['sectorKey', 'zonaGeografica', 'targetCliente', 'customerType'].every(k => typeof s1.payload[k] === 'string'));
  check('budget è numerico', typeof s1.payload.budget === 'number' && s1.payload.budget > 0);
  check('periodKey è presente', !!s1.payload.periodKey);
  check('competitor NON perso pur senza click su "Aggiungi"', s1.payload.competitors.length > 0);
  check('nome azienda risolto dal sito letto (non "la tua azienda")',
    getEl_textIncludes(s1.getEl, 'resultsIntro', 'NM Edilizia'));
  check('competitor mostrato con nome reale, non "Non hai indicato un competitor"',
    !getEl_textIncludes(s1.getEl, 'competitorBody', 'Non hai indicato un competitor') &&
    getEl_textIncludes(s1.getEl, 'competitorBody', 'Ciesse Costruzioni Milano'));
  check('nessun testo tecnico Pydantic visibile in UI',
    !getEl_textIncludes(s1.getEl, 'aiRetryError', 'Input should be a valid string'));
  check('nessun consiglio generico in "azioni" dopo fallimento AI totale',
    !/alzare il budget|creativit.{1,3} chiara|Misura le richieste/.test(s1.getEl('azioniBody').innerHTML));

  console.log('\n=== Scenario: stesso identico input, AI risponde 200 (successo) ===');
  const s2 = await eseguiScenario('200_ok');
  check('nome azienda risolto anche in caso di successo', getEl_textIncludes(s2.getEl, 'resultsIntro', 'NM Edilizia'));
  check('settore in intestazione aggiornato dal sotto-settore reale AI (non "il tuo settore")',
    getEl_textIncludes(s2.getEl, 'resultsIntro', 'Ristrutturazioni residenziali chiavi in mano'));
  check('competitor comparso correttamente col confronto reale',
    getEl_textIncludes(s2.getEl, 'competitorBody', 'Tu comunichi meglio'));
  check('azioni popolate con dati reali (non generiche)',
    getEl_textIncludes(s2.getEl, 'azioniBody', 'Il Giorno'));

  console.log('\n' + (esitiFalliti === 0 ? 'TUTTI I CONTROLLI PASS' : `${esitiFalliti} CONTROLLO/I FALLITO/I`));
  process.exit(esitiFalliti === 0 ? 0 : 1);
}

function getEl_textIncludes(getEl, id, testo) {
  const el = getEl(id);
  return (el.innerHTML || '').includes(testo) || (el.textContent || '').includes(testo);
}

main().catch(e => { console.error('ERRORE TEST:', e); process.exit(1); });
