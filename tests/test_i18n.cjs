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
