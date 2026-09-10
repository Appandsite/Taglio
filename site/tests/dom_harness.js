// Harness minimale per eseguire lo <script> di taglio-demo.html fuori dal
// browser (Node, modulo "vm"), con document/fetch/supabase/timer stubbati.
// Serve solo per test di regressione sul CONTRATTO DATI wizard -> SEARCH ->
// payload /api/generate-analysis -> rendering, non per test visivi/CSS.
const fs = require('fs');
const vm = require('vm');

function makeElementStore() {
  const store = new Map();
  function getEl(id) {
    if (!store.has(id)) {
      store.set(id, {
        id, _innerHTML: '', _textContent: '', value: '', style: { display: '' }, hidden: false,
        disabled: false, children: [],
        classList: { add() {}, remove() {}, toggle() {} },
        addEventListener() {},
        appendChild(child) { this.children.push(child); },
        prepend(child) { this.children.unshift(child); },
        get innerHTML() { return this._innerHTML; },
        set innerHTML(v) { this._innerHTML = v; this.children = []; },
        get textContent() { return this._textContent; },
        set textContent(v) { this._textContent = v; },
        querySelectorAll() { return []; },
      });
    }
    return store.get(id);
  }
  return { store, getEl };
}

function loadPage(htmlPath, campi) {
  const html = fs.readFileSync(htmlPath, 'utf-8');
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  const { getEl } = makeElementStore();
  Object.entries(campi).forEach(([id, val]) => { getEl(id).value = val; });
  const documentStub = {
    getElementById: (id) => getEl(id),
    querySelectorAll: () => [],
    querySelector: () => getEl('__anon__'),
    createElement: () => getEl('__created_' + Math.random()),
    addEventListener: () => {},
  };
  const sandbox = {
    console, document: documentStub,
    localStorage: { getItem: () => null, setItem: () => {} },
    fetch: () => Promise.reject(new Error('fetch non impostato: assegna ctx.fetch prima di chiamare runAnalysis()')),
    supabase: { createClient: () => ({ auth: { getSession: () => Promise.resolve({ data: {} }), onAuthStateChange: () => {} } }) },
    // setInterval è deferito a microtask (non invocato subito dentro alla
    // chiamata stessa): il chiamante referenzia la propria variabile
    // "interval" dentro la callback per poi fare clearInterval, che
    // altrimenti non sarebbe ancora assegnata (TDZ) se invocata subito.
    setInterval: (fn) => {
      let n = 0;
      const tick = () => { if (n++ < 10) { fn(); queueMicrotask(tick); } };
      queueMicrotask(tick);
      return 0;
    },
    clearInterval: () => {}, setTimeout: (fn) => { fn(); return 0; },
    URLSearchParams, location: { search: '', href: '' },
    Promise, JSON, Date,
  };
  sandbox.window = sandbox;
  const context = vm.createContext(sandbox);
  // `let currentProfile` a livello di script non diventa una proprietà del
  // sandbox con vm.runInContext (solo var/function declaration lo fanno):
  // appendiamo un helper nello STESSO script così condivide lo scope
  // lessicale e possiamo impostare currentProfile dall'esterno per il test.
  const scriptConHelper = script + '\nfunction __setTestProfile(p){ currentProfile = p; }\n';
  try { vm.runInContext(scriptConHelper, context); }
  catch (e) { console.log('(nota: esecuzione completa non riuscita per stub DOM mancanti —', e.message, ')'); }
  return { context, getEl };
}

module.exports = { loadPage };
