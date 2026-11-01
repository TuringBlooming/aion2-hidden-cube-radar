"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.join(__dirname, "../src/aion2_redbag_map");
const source = fs.readFileSync(path.join(root, "static/app.js"), "utf8");
const translations = fs.readFileSync(path.join(root, "static/i18n.js"), "utf8");
const startup = source.lastIndexOf('  $("language-select").value = locale();');
assert.ok(startup > 0);
const anchor = 1792764000;
let currentTime = anchor;

function harness(saved = {}) {
  const elements = new Map(), drawing = [], storage = new Map(Object.entries(saved));
  const context2d = new Proxy({}, { get(target, key) {
    if (key in target) return target[key];
    if (key === "measureText") return text => ({ width: text.length * 6 });
    return (...args) => drawing.push([key, ...args]);
  } });
  const element = () => {
    const classes = new Set();
    return {
      textContent: "", className: "", children: [], hidden: false, style: {}, handlers: new Map(),
      classList: { add: name => classes.add(name), remove: name => classes.delete(name),
        toggle(name, on) { if (on) classes.add(name); else classes.delete(name); }, contains: name => classes.has(name) },
      getContext() { return context2d; }, setAttribute() {},
      addEventListener(name, callback) { this.handlers.set(name, callback); },
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      getBoundingClientRect() { return { left: 0, top: 0, width: 900, height: 700 }; },
      setPointerCapture() {}, hasPointerCapture() { return false; }, releasePointerCapture() {},
    };
  };
  const document = {
    documentElement: { lang: "" }, querySelectorAll() { return []; },
    getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
    createElement: element, createTextNode(text) { return { textContent: text }; },
  };
  class Clock extends Date {
    constructor(...args) { super(...(args.length ? args : [currentTime * 1000])); }
    static now() { return currentTime * 1000; }
  }
  const context = vm.createContext({
    window: {}, document, Date: Clock,
    localStorage: { getItem(key) { return storage.get(key) ?? null; }, setItem(key, value) { storage.set(key, value); } },
    requestAnimationFrame() { return 1; }, setTimeout() { return 1; }, clearTimeout() {},
    Image: class {},
  });
  vm.runInContext(translations, context);
  const helpers = vm.runInContext(source.slice(0, startup) +
    "return {state, view, acceptData, playerPosition, playerOnMap, playerDistance, setFollowPlayer, followPlayerPosition, renderPlayer, renderSelected, renderClock, drawPlayer, drawCandidates, selectRecord, selectCandidate, zoom, focusTarget, targetEdge, drawTargetEdge, visibleRecords, hitMarker, toScreen, changeLanguage};})();", context);
  helpers.view.width = 900; helpers.view.height = 700;
  return { ...helpers, elements, drawing, storage, i18n: context.window.I18N };
}

const record = { id: "bag", map_id: 1110, template_id: 1301199, x: 1300, y: 2400, z: 90000,
  first_seen: anchor, last_seen: anchor, status: "seen", source: "live" };
const position = extra => ({ x: 1000, y: 2000, z: 300, map_id: 1110,
  observed_at: anchor, expires_at: anchor + 10, source: "self_status", identity_source: "stats_consensus", stale: false, ...extra });
const api = (player, extra = {}) => ({ session_id: "session-A", mode: "live", selected_map_id: 1110, current_map_id: 1110,
  map_source: "detected", capture: { phase: "observing", last_frame_at: currentTime, frames: 5 },
  maps: [1110, 1010].map(id => ({ id, x: 0, y: 0, w: 400000, h: 400000, name: "map" })),
  records: [{ ...record }], alert_seq: 0, sound: false, player, ...extra });

const ui = harness();
assert.doesNotThrow(() => ui.acceptData(api(undefined)), "old API without player remains usable");
assert.equal(ui.playerPosition(), null);
assert.equal(ui.elements.get("follow-player").disabled, true);
assert.match(ui.elements.get("player-status").textContent, /等待/);

ui.acceptData(api(position())); ui.state.selected = "bag"; ui.renderSelected();
assert.equal(ui.playerDistance(record), 5, "distance uses XY / 100, ignoring height");
assert.equal(ui.elements.get("selected-distance").textContent, "平面直线约 5 米");
ui.setFollowPlayer(true);
assert.equal(ui.view.cx, 1000); assert.equal(ui.view.cy, 2000);
const beforeZoom = ui.view.scale;
ui.zoom(1.5, 50, 50);
assert.ok(ui.view.scale > beforeZoom);
assert.equal(ui.view.cx, 1000, "zoom while following stays around the character");

currentTime = anchor + 2;
ui.acceptData(api(position({ x: 1600, y: 2100, observed_at: currentTime, expires_at: currentTime + 10 })));
assert.equal(ui.view.cx, 1600);
ui.acceptData(api(position({ x: 1800, y: 2200, observed_at: currentTime, expires_at: currentTime + 10 })));
assert.equal(ui.view.cx, 1800, "same packet timestamp can contain a later ordered position");
currentTime += 1; ui.renderClock();
assert.equal(ui.view.cx, 1800, "no movement is fabricated between observations");

const canvas = ui.elements.get("map-canvas");
canvas.handlers.get("pointerdown")({ button: 0, pointerId: 1, clientX: 100, clientY: 100 });
canvas.handlers.get("pointermove")({ pointerId: 1, clientX: 130, clientY: 120 });
assert.equal(ui.state.followPlayer, false, "manual dragging stops following");
canvas.handlers.get("pointerup")({ type: "pointerup", pointerId: 1, clientX: 130, clientY: 120 });
const draggedX = ui.view.cx;
ui.acceptData(api(position({ x: 5000, observed_at: currentTime, expires_at: currentTime + 10 })));
assert.equal(ui.view.cx, draggedX, "later positions do not snap back after manual dragging");

currentTime = anchor;
ui.acceptData(api(position())); ui.state.selected = "bag"; ui.setFollowPlayer(true);
ui.drawing.length = 0; ui.drawPlayer();
assert.ok(ui.drawing.some(call => call[0] === "lineTo"), "fresh selected target gets a straight reference line");
assert.ok(ui.drawing.some(call => call[0] === "arc"), "character is drawn as a dot, without inferred heading");
currentTime = anchor + 10; ui.renderClock();
assert.equal(ui.playerOnMap(true), null, "10 seconds is expired at the boundary");
assert.equal(ui.playerDistance(record), null);
assert.match(ui.elements.get("selected-distance").textContent, /已过期/);
assert.match(ui.elements.get("player-status").textContent, /已过期/);
ui.drawing.length = 0; ui.drawPlayer();
assert.ok(!ui.drawing.some(call => call[0] === "lineTo"), "expired position does not get a distance line");
assert.ok(ui.drawing.some(call => call[0] === "fillText" && call[1].includes("过期")));
const staleCenter = ui.view.cx;
ui.acceptData(api(position({ x: 8000, stale: true })));
assert.equal(ui.view.cx, staleCenter, "expired position cannot move the following view");

currentTime = anchor + 1;
ui.acceptData(api(position(), { selected_map_id: 1010 }));
assert.equal(ui.playerOnMap(), null, "player never appears on a browsed different map");
assert.equal(ui.playerDistance({ ...record, map_id: 1010 }), null);
ui.drawing.length = 0; ui.drawPlayer(); assert.equal(ui.drawing.length, 0);
assert.match(ui.elements.get("player-status").textContent, /阿尔特盖德/);
ui.acceptData(api(position(), { current_map_id: 1010 }));
assert.equal(ui.playerPosition(), null, "old player coordinates cannot follow a current-map reset");
for (const extra of [{ paused: true }, { capture: { phase: "waiting" } }, { capture: { phase: "error" } }, { player: null }]) {
  ui.acceptData(api(position(), extra)); assert.equal(ui.playerPosition(), null);
}
ui.acceptData(api(position())); ui.state.connected = false; ui.renderPlayer();
assert.equal(ui.playerPosition(), null, "HTTP disconnection removes the reference immediately");

ui.acceptData(api(position())); ui.setFollowPlayer(true); ui.state.selected = "bag";
const alertScale = ui.view.scale;
ui.acceptData(api(position({ x: 1100 }), { alert_seq: 1, latest_alert: { record_id: "bag", seq: 1, at: currentTime } }));
assert.equal(ui.state.flashId, "bag", "normal bag alerts still appear");
assert.equal(ui.state.selected, "bag", "a new live alert selects its target");
assert.equal(ui.state.followPlayer, true, "automatic new alerts preserve following");
assert.equal(ui.view.cx, 1100, "new alert keeps follow centered on the character");
assert.ok(ui.view.scale >= alertScale, "automatic focus never zooms out");
assert.ok(ui.view.scale >= Math.min(ui.view.width, ui.view.height - 70) / 24000, "an overview moves into about a 240m local view");
ui.selectRecord("bag", false);
assert.equal(ui.state.followPlayer, true, "selection without explicit focusing preserves following");
ui.zoom(2); const closerScale = ui.view.scale;
ui.selectRecord("bag", true);
assert.equal(ui.state.followPlayer, true, "locating a bag preserves following");
assert.equal(ui.view.cx, 1100, "locating a bag keeps the character centered");
assert.equal(ui.view.cy, 2000);
assert.equal(ui.view.scale, closerScale, "locating never discards a closer user zoom");
const alertSeq = ui.state.lastAlertSeq;
ui.acceptData(api(position({ x: 1200 }), { alert_seq: 1 }));
assert.equal(ui.state.lastAlertSeq, alertSeq, "position updates do not synthesize alerts");
assert.equal(ui.view.cx, 1200, "character centering continues after locating a bag");

const candidate = { id: "candidate-1", ordinal: 1, x: 6000, y: 7000, source_indices: [0] };
ui.state.candidates = { maps: { "1110": { points: [candidate], unique_count: 1, source_count: 1 } } };
ui.setFollowPlayer(true);
ui.selectCandidate(candidate, false);
assert.equal(ui.state.followPlayer, true);
ui.selectCandidate(candidate, true);
assert.equal(ui.state.followPlayer, true, "historical-point location also preserves following");
assert.equal(ui.view.cx, 1200); assert.equal(ui.view.cy, 2000);
ui.setFollowPlayer(false); ui.selectCandidate(candidate, true);
assert.equal(ui.view.cx, candidate.x); assert.equal(ui.view.cy, candidate.y);
ui.setFollowPlayer(true); ui.selectRecord("bag", false);

for (const language of ["zh-CN", "en", "ja", "ko"]) {
  ui.i18n.setLanguage(language); ui.renderPlayer(); ui.renderSelected();
  assert.equal(ui.elements.get("player-status").textContent, ui.i18n.t("角色参考位置"));
  assert.equal(ui.elements.get("follow-player").textContent, ui.i18n.t("跟随已开启"));
  assert.equal(ui.elements.get("selected-distance").textContent, ui.i18n.t("平面直线约 {distance} 米", { distance: 4 }));
}
assert.equal(ui.playerPosition().source, "self_status");

// A distant target remains explicit without shrinking the local map around the character.
currentTime = anchor + 2;
const far = { ...record, id: "far", x: 150000, y: 75000 };
const nearby = { ...record, id: "nearby", x: 4000, y: 3000 };
const targetUI = harness();
targetUI.acceptData(api(position(), { records: [far, nearby], alert_seq: 20, latest_alert: { seq: 20, record_id: "far", at: currentTime } }));
assert.equal(targetUI.state.selected, null, "initial history/API load never replays a new alert");
assert.equal(targetUI.state.flashId, null);
targetUI.setFollowPlayer(true); targetUI.zoom(6);
const localScale = targetUI.view.scale;
targetUI.state.filter = "opened";
targetUI.acceptData(api(position(), { records: [far, nearby], alert_seq: 21, latest_alert: { seq: 21, record_id: "far", at: currentTime } }));
assert.equal(targetUI.view.scale, localScale, "a far target cannot zoom the character view out");
assert.equal(targetUI.state.selected, "far");
assert.equal(targetUI.view.cx, 1000); assert.equal(targetUI.view.cy, 2000);
assert.deepEqual(Array.from(targetUI.visibleRecords(), item => item.id), ["far"], "the selected map target survives an unrelated status filter");
let edge = targetUI.targetEdge();
assert.ok(edge && edge.x > targetUI.view.width / 2 && edge.y > targetUI.view.height / 2, "off-screen arrow points in map-screen coordinates");
assert.ok(edge.x >= 30 && edge.x <= 870 && edge.y >= 90 && edge.y <= 610, "arrow stays inside the visible canvas");
targetUI.drawing.length = 0; targetUI.drawTargetEdge();
assert.ok(targetUI.drawing.some(call => call[0] === "fillText" && call[1].includes("画面外")));
assert.ok(targetUI.drawing.some(call => call[0] === "fillText" && call[1].includes("平面直线")));

currentTime = anchor + 11;
const staleView = { x: targetUI.view.cx, y: targetUI.view.cy, scale: targetUI.view.scale };
targetUI.acceptData(api(position({ x: 90000, stale: true }), { records: [far, nearby], alert_seq: 22, latest_alert: { seq: 22, record_id: "far", at: currentTime } }));
targetUI.selectRecord("far", true);
assert.equal(targetUI.state.followPlayer, true);
assert.equal(targetUI.view.cx, staleView.x, "expired reference preserves the existing center instead of jumping to a bag");
assert.equal(targetUI.view.cy, staleView.y);
assert.equal(targetUI.view.scale, staleView.scale);
assert.equal(targetUI.playerDistance(far), null);
targetUI.drawing.length = 0; targetUI.drawTargetEdge();
assert.ok(targetUI.drawing.some(call => call[0] === "fillText" && call[1].includes("仅表示当前地图画面方向")));
assert.ok(!targetUI.drawing.some(call => call[0] === "fillText" && call[1].includes("平面直线")), "expired references cannot create a live-distance assertion");

currentTime = anchor + 2;
targetUI.acceptData(api(position(), { records: [far, nearby], alert_seq: 23, latest_alert: { seq: 23, record_id: "nearby", at: currentTime } }));
assert.equal(targetUI.state.selected, "nearby");
assert.equal(targetUI.targetEdge(), null, "on-screen target uses its highlighted map marker, not an edge arrow");
const point = targetUI.toScreen(nearby.x, nearby.y);
assert.equal(targetUI.hitMarker(point.x, point.y).id, "nearby", "selected marker remains clickable despite the filter");
targetUI.setFollowPlayer(false); targetUI.selectRecord("far", true);
assert.equal(targetUI.view.cx, far.x); assert.equal(targetUI.view.cy, far.y);
assert.equal(targetUI.view.scale, localScale, "unfollowed target centering also preserves closer zoom");

targetUI.acceptData(api(position(), { records: [far, nearby], selected_map_id: 1010, alert_seq: 23 }));
assert.equal(targetUI.state.selected, null); assert.equal(targetUI.state.selectedCandidate, null);
assert.equal(targetUI.state.flashId, null); assert.equal(targetUI.state.alertRecord, null);
assert.equal(targetUI.targetEdge(far), null, "no old target direction crosses map boundaries");
assert.equal(targetUI.elements.get("alert-banner").hidden, true);
targetUI.acceptData(api(position(), { records: [far, nearby], session_id: "session-B", alert_seq: 99, latest_alert: { seq: 99, record_id: "far", at: currentTime } }));
assert.equal(targetUI.state.selected, null); assert.equal(targetUI.state.flashId, null);
assert.equal(targetUI.state.alertRecord, null, "a new server session clears the previous target and establishes a quiet alert baseline");
targetUI.acceptData(api(undefined, { records: [far, nearby], session_id: "session-B", alert_seq: 100, latest_alert: { seq: 100, record_id: "far", at: currentTime } }));
assert.equal(targetUI.state.selected, "far");
assert.equal(targetUI.view.cx, far.x, "without a reference position the new live bag can be centered directly");
assert.equal(targetUI.view.cy, far.y);
const otherMapBag = { ...far, id: "other-map", map_id: 1010, x: 8000, y: 9000 };
targetUI.acceptData(api(undefined, { records: [far, otherMapBag], selected_map_id: 1010, current_map_id: 1010, session_id: "session-B", alert_seq: 101, latest_alert: { seq: 101, record_id: "other-map", at: currentTime } }));
assert.equal(targetUI.state.selected, "other-map", "an alert in the same response as a map change is selected after the old map is cleared");
assert.equal(targetUI.view.cx, otherMapBag.x); assert.equal(targetUI.view.cy, otherMapBag.y);

targetUI.setFollowPlayer(false);
const dragCanvas = targetUI.elements.get("map-canvas");
dragCanvas.handlers.get("pointerdown")({ button: 0, pointerId: 2, clientX: 100, clientY: 100 });
dragCanvas.handlers.get("pointermove")({ pointerId: 2, clientX: 150, clientY: 140 });
const duringDrag = { x: targetUI.view.cx, y: targetUI.view.cy, scale: targetUI.view.scale };
const draggedAlert = { ...otherMapBag, id: "dragged-alert", x: 90000, y: 120000 };
targetUI.acceptData(api(undefined, { records: [otherMapBag, draggedAlert], selected_map_id: 1010, current_map_id: 1010, session_id: "session-B", alert_seq: 102, latest_alert: { seq: 102, record_id: "dragged-alert", at: currentTime } }));
assert.equal(targetUI.state.selected, "dragged-alert");
assert.equal(targetUI.view.cx, duringDrag.x, "automatic selection does not fight an active pointer drag");
assert.equal(targetUI.view.cy, duringDrag.y); assert.equal(targetUI.view.scale, duringDrag.scale);
assert.ok(targetUI.targetEdge());
dragCanvas.handlers.get("pointerup")({ type: "pointerup", pointerId: 2, clientX: 150, clientY: 140 });

const replayUI = harness();
replayUI.acceptData(api(undefined, { mode: "replay", records: [{ ...far, source: "replay" }] }));
const replayCenter = replayUI.view.cx, replayScale = replayUI.view.scale;
replayUI.acceptData(api(undefined, { mode: "replay", records: [{ ...far, source: "replay" }], alert_seq: 1, latest_alert: { seq: 1, record_id: "far", at: currentTime } }));
assert.equal(replayUI.state.selected, null, "historical replay does not trigger live autofocus");
assert.equal(replayUI.view.cx, replayCenter); assert.equal(replayUI.view.scale, replayScale);

// Candidates are available by default, remain separate from live records, and can be hidden explicitly.
const controlsUI = harness();
const upcoming = { ...nearby, id: "upcoming", status: "opened", refresh_estimated: true,
  first_seen: anchor - 3500, last_seen: anchor - 3500, opened_at: anchor - 3500 };
const controlsData = extra => api(position(), { records: [far, upcoming], ...extra });
controlsUI.acceptData(controlsData()); controlsUI.setFollowPlayer(true);
controlsUI.state.candidates = { maps: { "1110": { points: [candidate], unique_count: 1, source_count: 1 } } };
assert.equal(controlsUI.state.showCandidates, true, "new users see the candidate layer without changing a preference");
assert.equal(harness({ "redbag-show-candidates": "false" }).state.showCandidates, false, "an explicit saved hide preference is respected");
controlsUI.drawing.length = 0; controlsUI.drawCandidates();
assert.ok(controlsUI.drawing.some(call => call[0] === "lineTo"), "visible candidates draw their diamond markers");
let candidatePoint = controlsUI.toScreen(candidate.x, candidate.y);
assert.equal(controlsUI.hitMarker(candidatePoint.x, candidatePoint.y).id, candidate.id);
const controlsCanvas = controlsUI.elements.get("map-canvas");
controlsCanvas.handlers.get("pointerdown")({ button: 0, pointerId: 3, clientX: candidatePoint.x, clientY: candidatePoint.y });
controlsCanvas.handlers.get("pointerup")({ type: "pointerup", pointerId: 3, clientX: candidatePoint.x, clientY: candidatePoint.y });
assert.equal(controlsUI.state.selectedCandidate, candidate.id, "candidate map markers open their details");
assert.equal(controlsUI.elements.get("clear-target").disabled, false);
controlsUI.elements.get("candidates-button").handlers.get("click")();
assert.equal(controlsUI.state.showCandidates, false);
assert.equal(controlsUI.storage.get("redbag-show-candidates"), "false");
assert.equal(controlsUI.state.selectedCandidate, null);
assert.equal(controlsUI.elements.get("selected-card").hidden, true);
assert.equal(controlsUI.hitMarker(candidatePoint.x, candidatePoint.y), null, "hidden candidates cannot intercept map clicks");
controlsUI.drawing.length = 0; controlsUI.drawCandidates(); assert.equal(controlsUI.drawing.length, 0);
controlsUI.elements.get("candidates-button").handlers.get("click")();
assert.equal(controlsUI.storage.get("redbag-show-candidates"), "true");
assert.equal(controlsUI.hitMarker(candidatePoint.x, candidatePoint.y).id, candidate.id);
assert.equal(controlsUI.state.lastAlertSeq, 0, "candidate visibility never fabricates a live alert");

// A user can leave follow mode to inspect an upcoming point without later player packets pulling the view back.
controlsCanvas.handlers.get("pointerdown")({ button: 0, pointerId: 4, clientX: 100, clientY: 100 });
controlsCanvas.handlers.get("pointermove")({ pointerId: 4, clientX: 150, clientY: 140 });
controlsCanvas.handlers.get("pointerup")({ type: "pointerup", pointerId: 4, clientX: 150, clientY: 140 });
const browsedView = { x: controlsUI.view.cx, y: controlsUI.view.cy, scale: controlsUI.view.scale };
controlsUI.selectRecord("upcoming", false);
assert.equal(controlsUI.elements.get("selected-status").textContent, "即将刷新");
assert.equal(controlsUI.state.followPlayer, false);
controlsUI.acceptData(controlsData({ player: position({ x: 2500, y: 2600 }) }));
assert.deepEqual({ x: controlsUI.view.cx, y: controlsUI.view.cy, scale: controlsUI.view.scale }, browsedView,
  "inspecting an upcoming point after dragging leaves the manually chosen view intact");

// Clear-target and detail close clear the entire target presentation while keeping records and alert deduplication.
controlsUI.setFollowPlayer(true);
const liveAlert = { seq: 1, record_id: "far", at: currentTime };
controlsUI.acceptData(controlsData({ alert_seq: 1, latest_alert: liveAlert }));
assert.ok(controlsUI.targetEdge());
controlsUI.drawing.length = 0; controlsUI.drawPlayer();
assert.ok(controlsUI.drawing.some(call => call[0] === "lineTo"));
const observationsBeforeClear = JSON.stringify(controlsUI.state.records);
const clearView = { x: controlsUI.view.cx, y: controlsUI.view.cy, scale: controlsUI.view.scale };
const cancel = controlsUI.elements.get("clear-target").handlers.get("click");
assert.equal(cancel, controlsUI.elements.get("clear-selection").handlers.get("click"), "detail close and cancel-target have the same behavior");
cancel();
assert.equal(controlsUI.state.selected, null); assert.equal(controlsUI.state.selectedCandidate, null);
assert.equal(controlsUI.state.alertRecord, null); assert.equal(controlsUI.state.flashId, null); assert.equal(controlsUI.state.flashUntil, 0);
assert.equal(controlsUI.state.lastAlertSeq, 1, "clearing a target preserves the alert baseline");
assert.equal(JSON.stringify(controlsUI.state.records), observationsBeforeClear, "canceling never deletes or modifies observations");
assert.equal(controlsUI.state.followPlayer, true, "clearing a target does not toggle follow mode");
assert.deepEqual({ x: controlsUI.view.cx, y: controlsUI.view.cy, scale: controlsUI.view.scale }, clearView);
for (const id of ["selected-card", "alert-banner", "alert-locate", "marker-tooltip"]) {
  assert.equal(controlsUI.elements.get(id).hidden, true, `${id} is dismissed with the target`);
}
assert.equal(controlsUI.elements.get("clear-target").disabled, true);
assert.equal(controlsUI.targetEdge(), null);
controlsUI.drawing.length = 0; controlsUI.drawPlayer(); controlsUI.drawTargetEdge();
assert.ok(!controlsUI.drawing.some(call => call[0] === "lineTo"), "neither target line nor edge arrow survives cancellation");
controlsUI.elements.get("alert-locate").handlers.get("click")();
assert.equal(controlsUI.state.selected, null, "the dismissed alert no longer has a location target");
for (const language of ["zh-CN", "en", "ja", "ko"]) {
  controlsUI.changeLanguage(language);
  controlsUI.acceptData(controlsData({ alert_seq: 1, latest_alert: liveAlert }));
  controlsUI.renderClock();
  assert.equal(controlsUI.state.selected, null, "repeated polling and language changes cannot restore the same dismissed alert");
  assert.equal(controlsUI.state.alertRecord, null); assert.equal(controlsUI.state.flashId, null);
  assert.equal(controlsUI.targetEdge(), null); assert.equal(controlsUI.elements.get("alert-banner").hidden, true);
}
const newBag = { ...nearby, id: "new-bag" };
controlsUI.acceptData(controlsData({ records: [far, upcoming, newBag], alert_seq: 2,
  latest_alert: { seq: 2, record_id: newBag.id, at: currentTime } }));
assert.equal(controlsUI.state.selected, newBag.id, "the next new live alert can still select and highlight its target");
assert.equal(controlsUI.state.flashId, newBag.id); assert.equal(controlsUI.elements.get("clear-target").disabled, false);
controlsUI.selectCandidate(candidate, false);
controlsUI.elements.get("clear-selection").handlers.get("click")();
assert.equal(controlsUI.state.selectedCandidate, null); assert.equal(controlsUI.state.alertRecord, null);
assert.equal(controlsUI.elements.get("candidate-index").value, "");
assert.equal(controlsUI.state.candidates.maps["1110"].points.length, 1, "closing candidate details preserves its point data");
assert.equal(controlsUI.state.records.length, 3, "closing candidate details preserves live and historical records");
controlsUI.acceptData(controlsData({ records: [far, upcoming, newBag], alert_seq: 2,
  latest_alert: { seq: 2, record_id: newBag.id, at: currentTime } }));
assert.equal(controlsUI.state.selected, null); assert.equal(controlsUI.state.selectedCandidate, null);
console.log("Player reference UI: expiry, map/session isolation, follow/drag/zoom, 240m target focus, candidate default/toggle, upcoming-point browsing, target cancellation and poll deduplication, quiet history and four languages passed.");
