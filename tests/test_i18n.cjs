"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.join(__dirname, "../src/aion2_redbag_map");
const source = fs.readFileSync(path.join(root, "static/i18n.js"), "utf8");
const html = fs.readFileSync(path.join(root, "static/index.html"), "utf8");
const app = fs.readFileSync(path.join(root, "static/app.js"), "utf8");
const languages = ["zh-CN", "en", "ja", "ko"];
const storage = new Map();

function loadI18n(blockStorage = false) {
  const context = vm.createContext({
    window: {},
    document: { documentElement: { lang: "" }, querySelectorAll() { return []; } },
    localStorage: {
      getItem(key) { if (blockStorage) throw new Error("storage unavailable"); return storage.get(key) ?? null; },
      setItem(key, value) { if (blockStorage) throw new Error("storage unavailable"); storage.set(key, value); },
    },
  });
  vm.runInContext(source, context);
  return context.window.I18N;
}

const i18n = loadI18n();
assert.equal(i18n.language, "zh-CN");
const sourceKeys = Object.keys(i18n.catalogs["zh-CN"]).sort();
assert.ok(sourceKeys.length > 70, "static and dynamic translation catalog must be populated");
const placeholders = text => [...String(text).matchAll(/\{([A-Za-z0-9_]+)\}/g)].map(match => match[1]).sort();
for (const language of languages) {
  assert.deepEqual(Object.keys(i18n.catalogs[language]).sort(), sourceKeys, `${language}: keys`);
  i18n.setLanguage(language);
  assert.equal(i18n.language, language);
  assert.equal(storage.get("redbag-language"), language);
  for (const key of sourceKeys) {
    const translation = i18n.catalogs[language][key];
    assert.equal(typeof translation, "string", `${language}: ${key}`);
    assert.ok(translation.trim(), `${language}: empty translation for ${key}`);
    assert.deepEqual(placeholders(translation), placeholders(key), `${language}: placeholders in ${key}`);
    const values = Object.fromEntries(placeholders(key).map(name => [name, `<${name}>`]));
    const rendered = i18n.t(key, values);
    assert.equal(placeholders(rendered).length, 0, `${language}: unresolved placeholders in ${key}`);
  }
  assert.equal(loadI18n().language, language, "reload keeps the chosen language");
}

const decode = text => text.replaceAll("&quot;", '"').replaceAll("&#39;", "'").replaceAll("&lt;", "<").replaceAll("&gt;", ">").replaceAll("&amp;", "&");
let staticCount = 0;
for (const match of html.matchAll(/\bdata-i18n(?:-title|-aria-label|-placeholder)?="([^"]+)"/g)) {
  const key = decode(match[1]);
  assert.ok(Object.hasOwn(i18n.catalogs.en, key), `missing static translation: ${key}`);
  staticCount += 1;
}
assert.ok(staticCount > 70);
let dynamicCount = 0;
for (const match of app.matchAll(/\bt\(\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')/g)) {
  const key = vm.runInNewContext(match[1]);
  assert.ok(Object.hasOwn(i18n.catalogs.en, key), `missing dynamic translation: ${key}`);
  dynamicCount += 1;
}
assert.ok(dynamicCount > 50);
assert.doesNotThrow(() => loadI18n(true), "blocked browser storage must not break the map");
assert.ok(languages.includes(loadI18n(true).language));
for (const value of ["invalid", "<script>", "", null]) {
  storage.set("redbag-language", value);
  assert.ok(languages.includes(loadI18n().language), "invalid saved locale must fall back safely");
}
const maps = JSON.parse(fs.readFileSync(path.join(root, "maps.json"), "utf8"));
const originalMaps = JSON.stringify(maps);
for (const language of languages) {
  i18n.setLanguage(language);
  for (const map of maps) {
    const name = i18n.mapName(map);
    assert.ok(typeof name === "string" && name.trim(), `${language}: map ${map.id}`);
    if (language === "zh-CN" || language === "ja") assert.match(name, /[\p{Script=Han}\p{Script=Katakana}]/u, `${language}: map name must be localized`);
    if (language === "ko") assert.match(name, /\p{Script=Hangul}/u, "Korean map name must be localized");
    assert.equal(i18n.mapName({ ...map, id: String(map.id), name: "legacy name", english_name: "legacy name" }), name, "legacy source labels cannot override the language");
  }
}
assert.equal(JSON.stringify(maps), originalMaps, "translations do not rewrite coordinates or map IDs");
console.log(`Four-language catalog: ${sourceKeys.length} keys, ${staticCount} static bindings, ${dynamicCount} dynamic calls; placeholders and persistence passed.`);
console.log(`Map names: all ${maps.length} map entries localized in four languages; legacy names and map data preserved.`);

function languageHarness(saved = "zh-CN") {
  const elements = new Map(), savedValues = new Map([["redbag-language", saved]]), calls = [], messages = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, { hidden: true, textContent: "", value: "" });
    return elements.get(id);
  };
  const context = vm.createContext({
    window: {}, document: { documentElement: { lang: "" }, querySelectorAll() { return []; } },
    localStorage: { getItem: key => savedValues.get(key), setItem: (key, value) => savedValues.set(key, value) },
    $: element, clearTimeout() {}, setTimeout() { return 1; }, AbortSignal,
    recordsWithRefreshEstimates: rows => rows, fitMap() {}, showAlert() {}, followPlayerPosition() {},
    renderPlayer() {}, renderConnection() {}, renderMaps() {}, renderRecords() {}, requestPaint() {},
    renderClock() {}, renderAlert() {}, renderSelected() {}, localizedDetail: value => value,
    toast: value => messages.push(value),
    fetch(url, options) {
      return new Promise((resolve, reject) => calls.push({ url, options, resolve, reject }));
    },
  });
  vm.runInContext(source, context);
  const start = app.indexOf("  function acceptData("), end = app.indexOf('  $("zoom-in").addEventListener');
  assert.ok(start > 0 && end > start);
  const helpers = vm.runInContext(`(() => {
    const state = { selected: null, session: null, connected: false, lastAlertSeq: null };
    const view = {};
    let activeAlert = null, languageRequest = null, languageSyncing = false, languageRevision = 0;
    let alertTimer = null, pollTimer = null;
    const locale = () => window.I18N.language, t = key => window.I18N.t(key);
    ${app.slice(start, end)}
    return { state, acceptData, changeLanguage, poll, get revision() { return languageRevision; } };
  })()`, context);
  return { helpers, i18n: context.window.I18N, calls, messages, savedValues, elements };
}

const languageSnapshot = (language, extra = {}) => ({ session_id: "session-A", language,
  selected_map_id: 1110, maps: [], records: [], control_token: "local-test-token", ...extra });
const settle = () => new Promise(resolve => setImmediate(resolve));
const ok = call => call.resolve({ ok: true, json: async () => ({ ok: true }) });

async function checkLanguageSync() {
  const ui = languageHarness("ko"), { helpers } = ui;
  helpers.acceptData(languageSnapshot("en"));
  assert.equal(ui.i18n.language, "en", "connected service preference wins over saved browser language");
  assert.equal(ui.calls.length, 0, "connecting must not overwrite the shared language");
  helpers.acceptData(languageSnapshot("ja"));
  assert.equal(ui.i18n.language, "ja", "external desktop changes reach the web map");
  assert.equal(ui.savedValues.get("redbag-language"), "ja", "shared choice persists in browser storage");
  assert.equal(ui.elements.get("language-select").value, "ja");
  helpers.acceptData(languageSnapshot("invalid"));
  assert.equal(ui.i18n.language, "ja", "invalid server locale is ignored");
  helpers.acceptData(languageSnapshot("en"));

  const beforeChoice = helpers.revision;
  helpers.changeLanguage("ja");
  assert.equal(ui.calls.length, 1);
  assert.equal(JSON.parse(ui.calls[0].options.body).language, "ja");
  helpers.acceptData(languageSnapshot("en"), beforeChoice);
  assert.equal(ui.i18n.language, "ja", "a pre-selection poll cannot undo the chosen language");
  helpers.acceptData(languageSnapshot("en"));
  assert.equal(ui.i18n.language, "ja", "polls during a pending write cannot undo the selection");
  helpers.changeLanguage("en"); helpers.changeLanguage("ko");
  assert.equal(ui.calls.length, 1, "rapid choices do not create concurrent writes");
  ok(ui.calls[0]); await settle();
  assert.equal(ui.calls.length, 2);
  assert.equal(JSON.parse(ui.calls[1].options.body).language, "ko", "only the latest queued language is sent");
  const duringWrite = helpers.revision;
  ok(ui.calls[1]); await settle();
  helpers.acceptData(languageSnapshot("ja"), duringWrite);
  assert.equal(ui.i18n.language, "ko", "a poll started during the write stays stale after completion");
  helpers.acceptData(languageSnapshot("ko"));
  helpers.acceptData(languageSnapshot("en"));
  assert.equal(ui.i18n.language, "en", "later external changes still propagate");

  helpers.changeLanguage("ja");
  ui.calls[2].reject(new Error("unavailable")); await settle();
  assert.equal(ui.messages.length, 1, "failed writes show feedback");
  helpers.acceptData(languageSnapshot("en"));
  assert.equal(ui.i18n.language, "en", "failed writes do not lock out server updates");
  helpers.changeLanguage("ja"); ok(ui.calls[3]); await settle();
  helpers.acceptData(languageSnapshot("ja"));
  assert.equal(ui.i18n.language, "ja", "the same language can be retried after failure");

  helpers.changeLanguage("en"); helpers.changeLanguage("ko");
  helpers.acceptData(languageSnapshot("zh-CN", { session_id: "session-B", control_token: "new-local-token" }));
  assert.equal(ui.i18n.language, "zh-CN", "a replacement service has its own authoritative preference");
  ok(ui.calls[4]); await settle();
  assert.equal(ui.calls.length, 5, "queued choices for an old service are discarded");
  helpers.state.connected = false;
  helpers.changeLanguage("ko");
  assert.equal(ui.i18n.language, "ko", "offline local switching remains available");
  assert.equal(ui.savedValues.get("redbag-language"), "ko");
  assert.equal(ui.calls.length, 5, "offline choices never send controls");
  helpers.acceptData(languageSnapshot("ja", { session_id: "session-B" }));
  assert.equal(ui.i18n.language, "ja", "reconnection restores the shared preference without writing stale local state");

  const race = languageHarness();
  race.helpers.acceptData(languageSnapshot("en"));
  const polling = race.helpers.poll();
  race.helpers.changeLanguage("ko");
  ok(race.calls[1]); await settle();
  race.calls[0].resolve({ ok: true, json: async () => languageSnapshot("en") });
  await polling;
  assert.equal(race.i18n.language, "ko", "the real poll path retains its start revision across awaits");
  console.log("Shared language: server changes, serial latest-choice writes, stale polls, failure recovery, session replacement and offline selection passed.");
}

checkLanguageSync().catch(error => { console.error(error); process.exitCode = 1; });
