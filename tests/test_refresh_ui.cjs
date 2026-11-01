"use strict";

// Run the shipped rendering code against a minimal in-memory DOM and a fixed clock.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../src/aion2_redbag_map/static/app.js"), "utf8");
const translations = fs.readFileSync(path.join(__dirname, "../src/aion2_redbag_map/static/i18n.js"), "utf8");
const boundary = source.indexOf("  function asset(url) {");
assert.ok(boundary > 0, "UI helper boundary must exist");
const anchor = 1792764000;
let currentTime = anchor;

function harness() {
  const elements = new Map();
  let paints = 0;
  const element = () => ({
    textContent: "", className: "", children: [], hidden: false,
    classList: { toggle() {} }, getContext() { return {}; },
    setAttribute() {}, addEventListener() {},
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
  });
  const document = {
    documentElement: { lang: "" }, querySelectorAll() { return []; },
    getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
    createElement: element,
    createTextNode(text) { return { textContent: text }; },
  };
  class Clock extends Date {
    constructor(...args) { super(...(args.length ? args : [currentTime * 1000])); }
    static now() { return currentTime * 1000; }
  }
  const context = vm.createContext({
    window: {}, document, Date: Clock, localStorage: { getItem() { return null; }, setItem() {} },
    requestPaint() { paints += 1; }, notifyPermissionLabel() {},
  });
  vm.runInContext(translations, context);
  const helpers = vm.runInContext(source.slice(0, boundary) +
    "return {state, recordsWithRefreshEstimates, refreshWindow, displayStatus, renderClock, fullTime, clock, latestRecordTime, newestFirst, markerTooltip};})();", context);
  helpers.state.mapId = 1110;
  helpers.state.filter = "soon";
  helpers.state.selected = "record-1";
  helpers.state.connected = true;
  helpers.state.data = { capture: { phase: "observing" }, alert_seq: 7 };
  return { ...helpers, i18n: context.window.I18N, elements, paints: () => paints };
}

const record = { id: "record-1", map_id: 1110, template_id: 1301199,
  x: 100, y: 200, z: 300, spawned_at: anchor * 1000,
  first_seen: anchor - 20, received_at: anchor - 20, last_seen: anchor,
  status: "opened", opened_at: anchor, source: "live" };
const ui = harness();
ui.state.records = ui.recordsWithRefreshEstimates([record]);
const rows = ui.state.records;

// No new observations or API responses: the normal one-second renderer must
// both add and remove the existing marker from the upcoming-refresh filter.
const cases = [
  [0, false, 1], [299, false, 1], [3299.999, false, 1],
  [3300, true, 1], [3600, true, 1], [3899.999, true, 1],
  [3900, false, 2], [6899.999, false, 2], [6900, true, 2],
  [7200, true, 2], [7499.999, true, 2], [7500, false, 3],
  [10500, true, 3], [11100, false, 4],
  [86400, true, 24], [86700, false, 25],
];
for (const [elapsed, active, cycle] of cases) {
  currentTime = anchor + elapsed;
  ui.renderClock();
  const window = ui.refreshWindow(rows[0]);
  assert.equal(window.active, active, `window at ${elapsed}s`);
  assert.equal(window.cycle, cycle);
  assert.equal(ui.displayStatus(rows[0]), active ? "soon" : "opened");
  const listed = ui.elements.get("record-list").children.filter(child => child.className.startsWith("record-row"));
  assert.equal(listed.length, active ? 1 : 0, `filter at ${elapsed}s`);
  assert.equal(ui.elements.get("selected-status").textContent, active ? "即将刷新" : "已开启");
  assert.equal(ui.elements.get("refresh-expected-time").textContent, ui.fullTime(anchor + cycle * 3600));
  assert.equal(ui.elements.get("refresh-end-time").textContent, ui.fullTime(anchor + cycle * 3600 + 300));
  assert.ok(ui.elements.get("selected-time").textContent.includes("已开启"));
  assert.equal(rows[0].opened_at, anchor);
  assert.equal(rows[0].first_seen, anchor - 20, "first discovery remains historical data");
  assert.ok(ui.elements.get("selected-time").textContent.startsWith(`最新更新 ${ui.fullTime(anchor)}`));
  assert.equal(ui.state.data.alert_seq, 7);
}
assert.ok(ui.paints() >= 8, "time-based filter changes must repaint the map");
assert.equal(ui.recordsWithRefreshEstimates([record]), rows, "cache must not stop time-based updates");
assert.equal(record.refresh_anchor_at, undefined, "do not mutate the original observation");
assert.equal(ui.refreshWindow({ source_indices: [1] }), null, "static candidates have no timer");

currentTime = anchor + 3900;
const staleApiRecord = { ...record, refresh_estimated: true, refresh_anchor_at: record.first_seen,
  refresh_soon_at: record.first_seen + 3300, refresh_expected_at: record.first_seen + 3600, display_status: "soon" };
assert.equal(ui.displayStatus(staleApiRecord), "opened", "old backend status cannot keep an expired window active");
const reloaded = harness();
reloaded.state.records = reloaded.recordsWithRefreshEstimates([record]);
reloaded.renderClock();
assert.equal(reloaded.elements.get("selected-status").textContent, "已开启");
assert.equal(reloaded.state.records[0].refresh_anchor_at, anchor);
assert.ok(reloaded.elements.get("refresh-countdown").textContent.startsWith("距下次预警"));

// A later observation must move both the timer and the displayed time, including
// when the still-running server sends estimates based on its old first-seen rule.
const updated = { ...staleApiRecord, last_seen: anchor + 900 };
currentTime = anchor + 3300;
const updatedRows = reloaded.recordsWithRefreshEstimates([updated]);
assert.equal(updatedRows[0].refresh_anchor_at, anchor + 900);
assert.equal(reloaded.refreshWindow(updatedRows[0]).active, false, "a fresh update replaces the old imminent window");
assert.equal(reloaded.refreshWindow(updatedRows[0]).expectedAt, anchor + 4500);
assert.equal(updatedRows[0].first_seen, record.first_seen);
assert.equal(updatedRows[0].opened_at, record.opened_at);
assert.equal(updated.refresh_anchor_at, record.first_seen, "do not mutate old server responses");
assert.equal(reloaded.latestRecordTime({ first_seen: anchor, last_seen: Infinity, opened_at: true, received_at: "9999999999" }), anchor);
reloaded.state.records = updatedRows;
reloaded.state.filter = "all";
reloaded.renderClock();
assert.ok(reloaded.elements.get("selected-time").textContent.startsWith(`最新更新 ${ui.fullTime(anchor + 900)}`));
assert.ok(reloaded.markerTooltip(updatedRows[0]).includes(`${ui.clock(anchor + 900)} 更新`));
const updatedRow = reloaded.elements.get("record-list").children[0];
assert.equal(updatedRow.children[2].children[1].textContent, `${ui.clock(anchor + 900)} 更新`);
assert.equal(reloaded.state.data.alert_seq, 7, "recalculating estimates must not replay alerts");

// Shared observations of the same known spawn use the latest update, without
// propagating it to another map, elevation, or spawn cycle.
const duplicate = { ...record, id: "same-cycle", first_seen: anchor + 40, received_at: anchor + 40, last_seen: anchor + 1200 };
const newCycle = { ...record, id: "new-cycle", spawned_at: record.spawned_at + 3600000, first_seen: anchor + 3600, received_at: anchor + 3600, last_seen: anchor + 3600, opened_at: null, status: "seen" };
const otherMap = { ...record, id: "other-map", map_id: 1111, last_seen: anchor + 80 };
const otherFloor = { ...record, id: "other-floor", z: 2000, last_seen: anchor + 90 };
const joined = reloaded.recordsWithRefreshEstimates([record, duplicate, newCycle, otherMap, otherFloor]);
assert.equal(joined.find(item => item.id === record.id).refresh_anchor_at, anchor + 1200);
assert.equal(joined.find(item => item.id === duplicate.id).refresh_anchor_at, anchor + 1200);
assert.equal(joined.find(item => item.id === record.id).first_seen, record.first_seen);
assert.equal(joined.find(item => item.id === duplicate.id).first_seen, duplicate.first_seen);
assert.equal(joined.find(item => item.id === newCycle.id).refresh_anchor_at, anchor + 3600);
assert.equal(joined.find(item => item.id === otherMap.id).refresh_anchor_at, anchor + 80);
assert.equal(joined.find(item => item.id === otherFloor.id).refresh_anchor_at, anchor + 90);
const laterSeenEarlierUpdated = { ...record, id: "later-discovery", first_seen: anchor + 300, received_at: anchor + 300, last_seen: anchor + 400, opened_at: null };
assert.equal([laterSeenEarlierUpdated, updatedRows[0]].sort(reloaded.newestFirst)[0].id, updatedRows[0].id, "latest location uses update time, not discovery time");

// Switching the rendered language must leave observation and hourly behavior intact.
const recordsBefore = JSON.stringify(ui.state.records);
for (const [language, openedLabel, soonLabel] of [
  ["zh-CN", "已开启", "即将刷新"],
  ["en", "Opened", "Respawn soon"],
  ["ja", "開封済み", "まもなく再出現"],
  ["ko", "열림", "곧 재출현"],
]) {
  ui.i18n.setLanguage(language);
  ui.state.listKey = "";
  for (const [elapsed, active, cycle] of cases) {
    currentTime = anchor + elapsed;
    ui.renderClock();
    assert.equal(ui.refreshWindow(rows[0]).cycle, cycle, `${language}: hourly cycle`);
    assert.equal(ui.displayStatus(rows[0]), active ? "soon" : "opened", `${language}: state at ${elapsed}s`);
    assert.equal(ui.elements.get("selected-status").textContent, active ? soonLabel : openedLabel);
    assert.equal(ui.elements.get("refresh-expected-time").textContent, ui.fullTime(anchor + cycle * 3600));
    assert.ok(ui.elements.get("selected-time").textContent.includes(openedLabel));
    assert.equal(ui.state.data.alert_seq, 7, "translations must not trigger new alerts");
  }
  assert.equal(JSON.stringify(ui.state.records), recordsBefore, `${language}: records unchanged`);
}
console.log("Latest-update refresh UI: 16 hourly boundaries in four languages, changed timestamps, same-spawn grouping, cycle/map/floor isolation, list/tooltip/details, reload and legacy API passed.");
