"use strict";

// Execute shipped UI helpers with deferred HTTP responses; no actual audio or service.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.join(__dirname, "../src/aion2_redbag_map/static");
const source = fs.readFileSync(path.join(root, "app.js"), "utf8");
const translations = fs.readFileSync(path.join(root, "i18n.js"), "utf8");
const startup = source.lastIndexOf('  $("language-select").value = locale();');
assert.ok(startup > 0);
const settle = () => new Promise(resolve => setImmediate(resolve));

function harness() {
  const elements = new Map(), calls = [], sounds = [];
  const drawing = new Proxy({}, { get: (_, key) => key === "measureText" ? text => ({ width: text.length * 6 }) : () => {} });
  const element = () => ({ value: "", checked: false, textContent: "", hidden: false, style: {}, children: [],
    classList: { toggle() {}, add() {}, remove() {} },
    getContext() { return drawing; }, setAttribute() {}, addEventListener() {},
    append(...children) { this.children.push(...children); }, replaceChildren(...children) { this.children = children; },
    getBoundingClientRect() { return { left: 0, top: 0, width: 900, height: 700 }; },
  });
  const document = { documentElement: { lang: "" }, querySelectorAll() { return []; }, createElement: element,
    createTextNode(text) { return { textContent: text }; },
    getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
  };
  const context = vm.createContext({ window: {}, document, AbortSignal,
    localStorage: { getItem() { return null; }, setItem() {} },
    requestAnimationFrame() { return 1; }, setTimeout() { return 1; }, clearTimeout() {}, Image: class {},
    fetch(url, options) { return new Promise((resolve, reject) => calls.push({ url, options, resolve, reject })); },
    Audio: class { constructor(url) { this.url = url; sounds.push(this); } play() { this.played = true; return Promise.resolve(); } pause() { this.paused = true; } },
  });
  vm.runInContext(translations, context);
  const ui = vm.runInContext(source.slice(0, startup) + `return {
    state, view, acceptData, editAlertSettings, saveAlertSettings, previewSound, showAlert,
    get preferences() { return { confirmed: {...alertSettingsConfirmed}, draft: alertSettingsDraft && {...alertSettingsDraft}, dirty: alertSettingsDirty, pending: alertSettingsPending, revision: alertSettingsRevision }; }
  };})();`, context);
  ui.view.width = 900; ui.view.height = 700;
  return { ui, elements, calls, sounds, i18n: context.window.I18N };
}
const snapshot = extra => ({ session_id: "session-A", selected_map_id: 1110, maps: [], records: [],
  capture: { phase: "observing" }, mode: "live", control_token: "local-test-token", alert_seq: 0,
  sound: true, notification: { available: true }, ...extra });
const reply = (call, data = { ok: true }, ok = true) => call.resolve({ ok, json: async () => data });
const plain = value => JSON.parse(JSON.stringify(value));

async function main() {
  const h = harness(), { ui, elements } = h;
  ui.acceptData(snapshot());
  assert.deepEqual(plain(ui.preferences.confirmed), { sound_profile: "chime", sound_volume: 35, overlay_on_alert: false });
  ui.acceptData(snapshot({ sound_profile: "bell", sound_volume: 25, overlay_on_alert: true }));
  assert.equal(elements.get("sound-profile").value, "bell");
  assert.equal(elements.get("sound-volume-value").textContent, "25%");
  elements.get("sound-profile").value = "bubble"; elements.get("sound-volume").value = "18"; elements.get("overlay-on-alert").checked = false;
  ui.editAlertSettings();
  const beforeWrite = ui.preferences.revision;
  ui.acceptData(snapshot({ sound_profile: "alarm", sound_volume: 100 }), undefined, beforeWrite);
  assert.equal(elements.get("sound-profile").value, "bubble", "polls preserve unsaved controls");
  assert.equal(ui.preferences.confirmed.sound_profile, "bell", "editing does not mutate applied settings");
  const save = ui.saveAlertSettings();
  const duringWrite = ui.preferences.revision;
  assert.equal(h.calls.length, 1);
  assert.deepEqual(JSON.parse(h.calls[0].options.body), { action: "alert_settings", sound_profile: "bubble", sound_volume: 18, overlay_on_alert: false });
  ui.acceptData(snapshot(), undefined, duringWrite);
  assert.equal(ui.preferences.pending, true);
  reply(h.calls[0]); assert.equal(await save, true);
  ui.acceptData(snapshot({ sound_profile: "alarm", sound_volume: 100 }), undefined, duringWrite);
  assert.equal(ui.preferences.confirmed.sound_profile, "bubble", "a poll begun during save stays stale after completion");
  assert.equal(ui.preferences.dirty, false);
  ui.acceptData(snapshot({ sound_profile: "sonar", sound_volume: 40 }));
  assert.equal(elements.get("sound-profile").value, "sonar", "later shared desktop changes propagate");

  elements.get("sound-volume").value = "0"; ui.editAlertSettings();
  assert.equal(elements.get("preview-sound").disabled, true, "zero volume is a silent preview");
  assert.equal(elements.get("save-alert-settings").disabled, false);
  const failed = ui.saveAlertSettings(); h.calls[1].reject(new Error("offline"));
  assert.equal(await failed, false);
  assert.equal(ui.preferences.confirmed.sound_volume, 40, "failed writes do not apply settings");
  assert.equal(ui.preferences.draft.sound_volume, 0, "failed writes retain the editable draft");

  elements.get("sound-volume").value = "12"; ui.editAlertSettings();
  const preview = ui.previewSound();
  assert.deepEqual(JSON.parse(h.calls[2].options.body), { action: "preview_sound", sound_profile: "sonar", sound_volume: 12 });
  reply(h.calls[2], { ok: true, preview_url: "/api/alert-sound?profile=sonar&volume=12" });
  await preview;
  assert.equal(h.sounds.length, 1); assert.equal(h.sounds[0].volume, 1, "PCM is already volume-scaled by the local service");
  assert.equal(h.sounds[0].url, "/api/alert-sound?profile=sonar&volume=12");
  assert.equal(ui.preferences.dirty, true, "preview never saves the draft");
  assert.equal(ui.state.alert_seq, undefined, "preview does not inject a Hidden Cube observation");
  const native = ui.previewSound(); reply(h.calls[3]); await native;
  assert.equal(h.sounds.length, 1, "native previews do not also play browser audio");

  ui.acceptData(snapshot({ session_id: "session-B", sound_profile: "arcade", sound_volume: 30 }));
  assert.equal(ui.preferences.draft, null, "replacement service discards the previous service's draft");
  ui.showAlert({ test: true }); await settle();
  assert.equal(h.sounds.length, 1, "native live alerts are not double-played");
  ui.acceptData(snapshot({ session_id: "session-B", sound_profile: "arcade", sound_volume: 30, notification: { available: false } }));
  ui.showAlert({ test: true }); await settle();
  assert.equal(h.sounds.length, 2); assert.equal(h.sounds[1].url, "/api/alert-sound?profile=arcade&volume=30");
  ui.acceptData(snapshot({ session_id: "session-B", mode: "replay", notification: { available: false } }));
  ui.showAlert({ test: true }); await settle();
  assert.equal(h.sounds.length, 2, "history replay never plays an alert sound");
  ui.acceptData(snapshot({ session_id: "session-B", mode: "offline", notification: { available: false }, records: [{ id: "history-only", map_id: 1110, x: 100, y: 200, z: 0, status: "seen", source: "history" }] }));
  ui.showAlert({ record_id: "history-only" }); await settle();
  assert.equal(h.sounds.length, 2, "historical records never gain browser sound through the fallback path");

  for (const language of ["zh-CN", "en", "ja", "ko"]) {
    h.i18n.setLanguage(language);
    for (const key of ["提醒设置", "柔和音阶", "轻铃", "气泡", "街机", "声呐", "原版警铃", "提示音量", "仅在新背包提醒时显示悬浮窗", "试听提示音", "保存设置"]) {
      assert.equal(typeof h.i18n.t(key), "string"); assert.ok(h.i18n.t(key).trim());
    }
  }
  console.log("Alert settings UI: defaults, shared updates, drafts, stale polls, save failures, mute, native/browser previews, no double audio, replay isolation and four languages passed.");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
