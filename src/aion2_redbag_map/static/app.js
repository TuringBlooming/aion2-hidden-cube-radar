"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const t = (key, params) => window.I18N.t(key, params);
  const mapName = map => window.I18N.mapName(map);
  const locale = () => window.I18N.language;
  const canvas = $("map-canvas"), stage = $("map-stage"), ctx = canvas.getContext("2d");
  const state = { data: null, connected: false, busy: false, filter: "all", selected: null, selectedCandidate: null, candidates: null, candidateError: false, showCandidates: true, mapId: null, maps: [], records: [], grid: true, lastAlertSeq: null, session: null, alertRecord: null, flashUntil: 0, flashId: null, mapKey: "", listKey: "", notify: false, followPlayer: false, playerFollowKey: null, playerRenderKey: null };
  const view = { width: 0, height: 0, dpr: 1, cx: 0, cy: 0, scale: 1, fit: 1, ready: false, pointer: null, hover: null };
  const assets = new Map();
  let repaintPending = false, toastTimer = null, alertTimer = null, pollTimer = null;
  let activeAlert = null, languageSyncKey = null;
  let legacyRecordsKey = null, legacyRecords = null;
  try { state.notify = localStorage.getItem("redbag-desktop-notifications") === "true"; } catch (_) {}
  try { state.showCandidates = localStorage.getItem("redbag-show-candidates") !== "false"; } catch (_) {}

  const number = value => Number.isFinite(Number(value)) ? Math.round(Number(value)).toLocaleString("en-US") : "—";
  const stamp = value => Number(value) > 0 ? Number(value) : 0;
  const latestRecordTime = item => Math.max(0, ...[item.last_seen, item.opened_at, item.received_at, item.refresh_anchor_at, item.first_seen].filter(value => typeof value === "number" && Number.isFinite(value) && value > 0));
  const newestFirst = (left, right) => latestRecordTime(right) - latestRecordTime(left);
  const clock = seconds => stamp(seconds) ? new Date(Number(seconds) * 1000).toLocaleTimeString(locale(), { timeZone: "Asia/Shanghai", hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
  const fullTime = seconds => stamp(seconds) ? new Date(Number(seconds) * 1000).toLocaleString(locale(), { timeZone: "Asia/Shanghai", hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
  const now = () => Date.now() / 1000;
  const currentMap = () => state.maps.find(item => String(item.id) === String(state.mapId)) || { id: state.mapId, name: t("未知地图"), x: -408000, y: -408000, w: 816000, h: 816000, tiles: [8, 8] };
  const mapRecords = () => state.records.filter(item => !item.superseded_by && String(item.map_id) === String(state.mapId) && Number.isFinite(Number(item.x)) && Number.isFinite(Number(item.y)));
  const selectedRecord = () => state.records.find(item => !item.superseded_by && String(item.id) === String(state.selected));
  const candidateMap = () => state.candidates?.maps?.[String(state.mapId)];
  const mapCandidates = () => candidateMap()?.points || [];
  const selectedCandidate = () => mapCandidates().find(item => item.id === state.selectedCandidate);
  const hasRefreshEstimate = item => !item.source_indices && item.refresh_estimated === true && latestRecordTime(item) > 0;
  const displayStatus = item => refreshWindow(item)?.active && !item.superseded_by ? "soon" : item.status;
  const colorFor = item => ({ seen: "#c8393e", used: "#c18b32", opened: "#738b78", unknown: "#7e8998", soon: "#8650b8" }[displayStatus(item)] || "#7e8998");
  const rawStatusFor = item => ({ seen: t("收到记录"), used: t("有人交互"), opened: t("已开启"), unknown: t("状态未知") }[item.status] || t("状态未知"));
  const statusFor = item => ({ seen: now() - stamp(item.last_seen || item.first_seen) < 90 ? t("刚收到") : t("此前收到"), used: t("有人交互"), opened: t("已开启"), unknown: t("状态未知"), soon: t("即将刷新") }[displayStatus(item)] || t("状态未知"));
  const sourceFor = item => item.source === "history" ? t("历史记录") : item.source === "replay" ? t("回放记录") : t("本机接收");
  const inFilter = item => state.filter === "all" || (state.filter === "pending" && ["seen", "used"].includes(displayStatus(item))) || state.filter === displayStatus(item);
  const visibleRecords = () => mapRecords().filter(item => inFilter(item) || String(item.id) === String(state.selected));
  const safeElement = (tag, className, text) => { const element = document.createElement(tag); if (className) element.className = className; if (text !== undefined) element.textContent = text; if (["▶", "Ⅱ", "♩", "♪", "✓", "↗", "◇"].includes(text)) element.setAttribute("aria-hidden", "true"); return element; };

  function localizedDetail(value) {
    const source = String(value || "");
    const exact = t(source);
    if (exact !== source) return exact;
    for (const catalog of Object.values(window.I18N.catalogs)) {
      const key = Object.keys(catalog).find(candidate => catalog[candidate] === source);
      if (key) return t(key);
    }
    if (locale() === "zh-CN") return source;
    const formats = [
      [/^采集已停止：(.+)$/, "采集已停止：{reason}"],
      [/^历史记录未能读取，已尽可能保留原文件：(.+)$/, "历史记录未能读取，已尽可能保留原文件：{reason}"],
      [/^历史记录暂时无法保存：(.+)$/, "历史记录暂时无法保存：{reason}"],
      [/^警铃未能播放：(.+)。请检查工具音量或警铃文件。$/, "警铃未能播放：{reason}。请检查工具音量或警铃文件。"],
      [/^无法读取 Npcap 网络接口：(.+)$/, "无法读取 Npcap 网络接口：{reason}"],
      [/^无法打开接收接口：(.+)$/, "无法打开接收接口：{reason}"],
      [/^系统通知不可用：(.+)$/, "系统通知不可用：{reason}"]
    ];
    for (const [pattern, key] of formats) { const match = source.match(pattern); if (match) return t(key, { reason: localizedDetail(match[1]) }); }
    if (/timeout|timed out|aborted/i.test(source)) return t("本地连接超时，请稍后重试。");
    if (/failed to fetch|networkerror|network error/i.test(source)) return t("本地连接不可用，请检查工具是否正在运行。");
    return /[\u3400-\u9fff]/.test(source) ? t("详细状态暂时无法翻译，请查看本机日志。") : source;
  }

  function toast(message) { $("toast").textContent = message; $("toast").hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { $("toast").hidden = true; }, 3500); }
  function ageLabel(value) { const age = Math.max(0, now() - stamp(value)); if (!stamp(value)) return t("尚未收到"); if (age < 3) return t("刚收到数据"); if (age < 60) return t("{count} 秒前", { count: Math.floor(age) }); if (age < 3600) return t("{count} 分钟前", { count: Math.floor(age / 60) }); return t("{count} 小时前", { count: Math.floor(age / 3600) }); }
  function playerPosition() {
    const player = state.data?.player;
    if (!state.connected || state.data?.paused || /paused|waiting|stopped|error|closed|disconnected/.test(String(state.data?.capture?.phase || "")) || !player || typeof player !== "object") return null;
    if (![player.x, player.y, player.z, player.observed_at, player.expires_at].every(value => typeof value === "number" && Number.isFinite(value)) || player.observed_at <= 0 || player.expires_at <= player.observed_at || [player.x, player.y, player.z].some(value => Math.abs(value) > 1000000)) return null;
    if (String(player.map_id) !== String(state.data?.current_map_id)) return null;
    return { ...player, stale: player.stale === true || now() >= Math.min(player.expires_at, player.observed_at + 10) };
  }
  function playerOnMap(freshOnly = false) {
    const player = playerPosition();
    return player && String(player.map_id) === String(state.mapId) && (!freshOnly || !player.stale) ? player : null;
  }
  function playerDistance(item) {
    const player = playerOnMap(true);
    if (!player || !item || String(item.map_id ?? state.mapId) !== String(player.map_id)) return null;
    return Math.hypot(item.x - player.x, item.y - player.y) / 100;
  }
  function followPlayerPosition(force = false) {
    const player = state.followPlayer && playerOnMap(true);
    if (!player) return false;
    const key = JSON.stringify([state.session, player.map_id, player.observed_at, player.x, player.y, player.z]);
    if (force || key !== state.playerFollowKey) {
      view.cx = player.x; view.cy = player.y; state.playerFollowKey = key; requestPaint();
    }
    return true;
  }
  function setFollowPlayer(enabled) {
    state.followPlayer = !!enabled; state.playerFollowKey = null;
    if (state.followPlayer && playerOnMap(true)) {
      view.scale = Math.min(view.fit * 100, Math.max(view.scale, Math.min(view.width, view.height - 70) / 80000));
      followPlayerPosition(true);
    }
    renderPlayer(); requestPaint();
  }
  function renderPlayer() {
    const player = playerPosition(), local = playerOnMap();
    const phase = state.data?.position_diagnostics?.state;
    const key = JSON.stringify([locale(), local?.x, local?.y, local?.z, local?.observed_at, local?.stale, state.followPlayer]);
    if (key !== state.playerRenderKey) { state.playerRenderKey = key; requestPaint(); }
    $("player-status").textContent = phase === "awaiting_map" ? t("地图待确认，切换线路后恢复定位") : phase === "awaiting_stream" ? t("等待连接恢复，恢复后自动更新定位") : !player ? t("等待地图与角色参考位置") : !local ? t("角色参考位置在{map}", { map: mapName(state.maps.find(map => String(map.id) === String(player.map_id)) || { id: player.map_id }) }) : local.stale ? t("位置已过期，等待新记录") : t("角色参考位置");
    $("player-age").textContent = local ? t("最近位置 · {age}", { age: ageLabel(local.observed_at) }) : "";
    $("player-coordinates").textContent = local ? `X ${number(local.x)} · Y ${number(local.y)} · Z ${number(local.z)}` : "";
    $("player-bar").classList.toggle("stale", !!local?.stale);
    $("player-bar").classList.toggle("unavailable", !local);
    $("follow-player").textContent = state.followPlayer ? t("跟随已开启") : t("跟随角色");
    $("follow-player").disabled = !state.followPlayer && !playerOnMap(true);
    $("follow-player").classList.toggle("active", state.followPlayer);
    $("follow-player").setAttribute("aria-pressed", String(state.followPlayer));
  }
  function markerTooltip(item) {
    const heading = item.source_indices ? t("历史隐藏背包候选 #{number}", { number: item.ordinal }) : t("红色背包 · {status}", { status: statusFor(item) });
    const detail = item.source_indices ? t("颜色 / 当前状态未知 · 点击查看") : t("{time} 更新 · 点击查看", { time: clock(latestRecordTime(item)) });
    return `${heading}\nX ${number(item.x)}  Y ${number(item.y)}\n${detail}`;
  }
  function renderClock() { const d = new Date(); $("clock-time").textContent = d.toLocaleTimeString(locale(), { timeZone: "Asia/Shanghai", hour12: false }); $("clock-date").textContent = d.toLocaleDateString(locale(), { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" }); renderPlayer(); if (state.data) { renderConnection(); const previousListKey = state.listKey; renderRecords(); if (state.listKey !== previousListKey) requestPaint(); if (view.hover && !$("marker-tooltip").hidden) { const item = mapRecords().find(record => String(record.id) === String(view.hover)) || mapCandidates().find(candidate => candidate.id === view.hover); if (item) $("marker-tooltip").textContent = markerTooltip(item); } } }
  function remainingTime(deadline) { const seconds = Math.max(0, Math.ceil(stamp(deadline) - now())); return t("{minutes} 分 {seconds} 秒", { minutes: Math.floor(seconds / 60), seconds: String(seconds % 60).padStart(2, "0") }); }
  function refreshWindow(item, at = now()) {
    if (!hasRefreshEstimate(item)) return null;
    const period = 60 * 60, margin = 5 * 60;
    const anchor = latestRecordTime(item);
    const cycle = Math.max(1, Math.floor((at - anchor - margin) / period) + 1);
    const expectedAt = anchor + cycle * period;
    const startAt = expectedAt - margin, endAt = expectedAt + margin;
    return { cycle, startAt, expectedAt, endAt, active: at >= startAt && at < endAt };
  }
  function refreshCountdown(item) {
    const window = refreshWindow(item);
    if (!window) return t("暂无刷新估算");
    if (now() < window.startAt) return t("距下次预警 {time}", { time: remainingTime(window.startAt) });
    if (now() < window.expectedAt) return t("距预计刷新 {time}", { time: remainingTime(window.expectedAt) });
    return t("预计时间已到，提示还剩 {time}", { time: remainingTime(window.endAt) });
  }
  function currentRecordFor(id) {
    let item = state.records.find(record => String(record.id) === String(id));
    const visited = new Set();
    while (item?.superseded_by && !visited.has(String(item.id))) { visited.add(String(item.id)); item = state.records.find(record => String(record.id) === String(item.superseded_by)); }
    return item && !item.superseded_by ? item : null;
  }
  function recordsWithRefreshEstimates(records) {
    const key = JSON.stringify(records);
    if (key === legacyRecordsKey) return legacyRecords;
    const positiveTime = value => typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;
    const sameLocation = (left, right) => left.map_id === right.map_id && left.template_id === right.template_id && Math.hypot(left.x - right.x, left.y - right.y, left.z - right.z) <= 300;
    const cycleOrder = item => [positiveTime(item.spawned_at) || (positiveTime(item.refresh_anchor_at) || 0) * 1000, positiveTime(item.received_at) || positiveTime(item.first_seen) || 0];
    const compareCycles = (left, right) => { const a = cycleOrder(left), b = cycleOrder(right); return a[0] - b[0] || a[1] - b[1]; };
    const rows = records.map(item => ({ ...item }));
    const observations = rows.filter(item => !item.source_indices && ["seen", "used", "opened", "unknown"].includes(item.status) && positiveTime(item.map_id) && positiveTime(item.template_id) && [item.x, item.y, item.z].every(value => typeof value === "number" && Number.isFinite(value)));
    // Apply the latest-time rule to both old and new servers, preserving raw history.
    for (const item of observations) item.refresh_anchor_at = positiveTime(latestRecordTime(item));
    const previous = [];
    for (const item of [...observations].sort(compareCycles)) {
      const sameCycle = previous.filter(old => sameLocation(old, item) && positiveTime(item.spawned_at) !== null && old.spawned_at === item.spawned_at);
      const anchors = [...sameCycle, item].map(old => positiveTime(old.refresh_anchor_at)).filter(value => value !== null);
      if (anchors.length) { const anchor = Math.max(...anchors); for (const old of [...sameCycle, item]) old.refresh_anchor_at = anchor; }
      const nearby = previous.filter(old => old.id !== item.id && sameLocation(old, item));
      const group = [...nearby, item], winner = group.reduce((latest, candidate) => compareCycles(candidate, latest) > 0 ? candidate : latest);
      for (const old of group) { if (old.id === winner.id) delete old.superseded_by; else old.superseded_by = winner.id; }
      previous.push(item);
    }
    for (const item of observations) {
      const anchor = positiveTime(item.refresh_anchor_at);
      item.refresh_estimated = anchor !== null;
      const window = refreshWindow(item);
      item.refresh_soon_at = window?.startAt ?? null;
      item.refresh_expected_at = window?.expectedAt ?? null;
      item.refresh_window_end_at = window?.endAt ?? null;
      item.refresh_cycle = window?.cycle ?? null;
      item.display_status = displayStatus(item);
    }
    legacyRecordsKey = key; legacyRecords = rows;
    return rows;
  }

  function renderConnection() {
    const capture = state.data?.capture || {}, phase = String(capture.phase || "").toLowerCase(), replay = state.data?.mode === "replay";
    const paused = !!state.data?.paused || phase === "paused";
    const receiving = state.connected && !paused && /captur|running|listening|live|active|receiv|observing/.test(phase) && stamp(capture.last_frame_at) > 0 && now() - capture.last_frame_at < 8;
    let label = t("等待游戏数据"), mode = "waiting", chip = t("等待");
    if (!state.connected) { label = state.data ? t("已与工具断开连接") : t("正在连接工具"); mode = "error"; chip = t("离线"); }
    else if (replay) { label = /error|fail/.test(phase) ? t("历史回放失败") : phase === "replay_done" ? t("历史回放已结束") : t("正在回放历史采样"); chip = t("回放"); if (/error|fail/.test(phase)) mode = "error"; }
    else if (state.data?.mode === "offline") { label = t("历史点位浏览"); chip = t("离线"); }
    else if (paused) { label = t("已暂停接收"); chip = t("暂停"); }
    else if (/error|fail|unsupported/.test(phase)) { label = t("接收需要处理"); mode = "error"; chip = t("异常"); }
    else if (/stopped|shutdown|closed/.test(phase)) { label = t("接收已停止"); mode = "error"; chip = t("停止"); }
    else if (/replay/.test(phase)) { label = phase === "replay_done" ? t("历史回放已结束") : t("正在回放历史采样"); chip = t("回放"); }
    else if (receiving) { label = t("正在解析游戏记录"); mode = "receiving"; chip = t("接收中"); }
    else if (/wait.*game|no.game/.test(phase)) { label = t("等待游戏运行"); }
    else if (stamp(capture.last_packet_at) > 0 && now() - capture.last_packet_at < 8) { label = t("收到连接数据，待同步"); chip = t("待同步"); }
    $("connection-label").textContent = label;
    $("connection-dot").className = `status-dot ${mode}`;
    $("live-chip").textContent = chip;
    $("connection-detail").textContent = !state.connected ? (state.data ? t("本地连接中断，地图保留最后记录；当前状态无法确认。") : t("正在读取本地工具状态…")) : [capture.detail ? localizedDetail(capture.detail) : t("正常跑图即可。收到红色背包记录时会提醒你。"), state.data?.storage_error && localizedDetail(state.data.storage_error), state.data?.sound === false ? null : state.data?.notification?.sound_error && localizedDetail(state.data.notification.sound_error)].filter(Boolean).join(" ");
    $("packet-count").textContent = t("{count} 条解析记录", { count: number(capture.frames || 0) });
    $("packet-age").textContent = ageLabel(capture.last_frame_at);
    $("pause-button").replaceChildren(safeElement("span", "", paused ? "▶" : "Ⅱ"), document.createTextNode(" " + (paused ? t("继续接收") : t("暂停接收"))));
    $("sound-button").replaceChildren(safeElement("span", "", state.data?.sound === false ? "♩" : "♪"), document.createTextNode(" " + (state.data?.sound === false ? t("警铃关闭") : state.data?.notification?.sound_error ? t("警铃未播放") : t("警铃开启"))));
    $("sound-button").title = state.data?.notification?.sound_error ? localizedDetail(state.data.notification.sound_error) : t("三组双音警铃，约 3 秒。音量跟随此工具的系统音量，不调整游戏或系统音量。");
    for (const id of ["pause-button", "sound-button", "test-alert-button", "stop-button", "map-select"]) $(id).disabled = !state.connected || state.busy;
    if (state.data?.mode === "offline" || String(capture.detail || "").startsWith("离线检查模式")) { $("pause-button").disabled = true; $("pause-button").textContent = t("仅浏览地图"); $("pause-button").title = t("当前页面没有启动实时采集"); }
    renderStorage();
    notifyPermissionLabel();
  }

  function storageSummary() {
    const storage = state.data?.storage && typeof state.data.storage === "object" ? state.data.storage : null;
    const fallback = {};
    for (const item of state.records) { const key = String(item.map_id); fallback[key] = (fallback[key] || 0) + 1; }
    const mapCounts = { ...fallback };
    if (storage?.map_counts && typeof storage.map_counts === "object") for (const [id, count] of Object.entries(storage.map_counts)) if (Number.isInteger(count) && count >= 0) mapCounts[id] = count;
    const total = Number.isInteger(storage?.record_count) && storage.record_count >= 0 ? storage.record_count : state.records.length;
    const current = mapCounts[String(state.mapId)] || 0;
    return { storage, mapCounts, total, current, other: Math.max(0, total - current) };
  }

  function renderStorage() {
    const { storage, total, current, other } = storageSummary();
    const error = storage?.error || state.data?.storage_error;
    const supported = !!storage && typeof storage.directory === "string" && !!storage.directory;
    const enabled = supported && state.connected && !state.busy;
    $("storage-count").textContent = !state.data ? t("载入中") : supported && !error ? t("已存 {count} 条", { count: number(total) }) : t("已有 {count} 条", { count: number(total) });
    $("storage-warning").hidden = !error;
    $("storage-panel").classList.toggle("has-error", !!error);
    $("storage-note").textContent = !state.data ? t("正在读取存档信息…") : !supported ? t("当前版本未提供存档位置。请使用更新后的工具查看。") : !state.connected ? t("连接已断开，下方为最后读取的存档信息。") : storage.readonly ? t("正在查看本地存档，原有记录和时间保持不变。") : storage.recovered_from ? t("已从本地备份恢复记录；后续变化会继续保存。") : t("记录自动保存，重启工具后继续读取。存档包含以前刷新周期的记录。");
    $("storage-map-count").textContent = state.data ? t("本图 {count} 条 · 其他地图 {other} 条", { count: number(current), other: number(other) }) : "";
    $("storage-meta").hidden = !supported;
    $("storage-path").textContent = supported ? storage.history_file || storage.directory : "";
    $("storage-path").title = supported ? storage.history_file || storage.directory : "";
    $("storage-saved-at").textContent = stamp(storage?.last_saved_at) ? fullTime(storage.last_saved_at) : t("尚未保存");
    $("storage-backups").textContent = t("{count} 份", { count: number(storage?.backup_count || 0) });
    $("storage-error").hidden = !error;
    $("storage-error").textContent = error ? t("存档需要处理：{reason}", { reason: localizedDetail(error) }) : "";
    $("storage-actions").hidden = !supported;
    $("open-data-folder").disabled = !enabled || !state.data?.control_token;
    $("export-history").setAttribute("aria-disabled", String(!enabled));
    $("export-history").tabIndex = enabled ? 0 : -1;
  }

  function renderMaps() {
    const map = currentMap(), { mapCounts } = storageSummary(), key = JSON.stringify([locale(), state.maps.map(item => [item.id, item.name, item.english_name, mapCounts[String(item.id)] || 0])]);
    if (key !== state.mapKey) { $("map-select").replaceChildren(...state.maps.map(item => { const count = mapCounts[String(item.id)] || 0; const option = safeElement("option", "", count ? t("{map} · {count} 条", { map: mapName(item), count: number(count) }) : mapName(item)); option.value = item.id; return option; })); state.mapKey = key; }
    $("map-select").value = String(state.mapId);
    $("map-select").title = t("{map} · {count} 条", { map: mapName(map), count: number(mapCounts[String(state.mapId)] || 0) });
    $("map-title").textContent = mapName(map);
    const current = state.maps.find(item => String(item.id) === String(state.data?.current_map_id));
    if (state.data?.mode === "replay") $("map-source").textContent = t("历史采样回放 · 非当前游戏状态");
    else if (state.data?.mode === "offline") $("map-source").textContent = t("历史点位浏览 · 当前刷新状态未知");
    else if (state.data?.map_source === "detected") $("map-source").textContent = String(state.mapId) === String(state.data.current_map_id) ? t("当前地图已自动识别 · 线路未识别") : t("浏览地图 · 当前在{map}", { map: current ? mapName(current) : t("其他地图") });
    else $("map-source").textContent = t("当前地图手动指定 · 线路未识别");
    renderCandidates();
  }

  function renderCandidates() {
    const map = candidateMap(), count = map?.unique_count || 0;
    $("candidate-count").textContent = state.candidateError ? t("载入失败") : !state.candidates ? t("载入中") : t("{count} 个位置", { count: number(count) });
    $("candidate-layer-count").textContent = state.candidateError ? t("重试") : state.candidates ? number(count) : "…";
    $("candidate-description").textContent = state.candidateError ? t("历史点位暂时无法载入。点击地图上的“历史点位”重试。") : !state.candidates ? t("正在载入 A2Helper 的历史点位…") : count ? t("A2Helper 0.1.25 · {count} 条记录。{merged}颜色与当前刷新状态未知。", { count: number(map.source_count), merged: map.source_count > count ? t("重复坐标已合并。") : "" }) : t("A2Helper 0.1.25 没有这张地图的隐藏背包点位记录；不代表地图中没有背包。");
    $("candidates-button").classList.toggle("active", state.showCandidates);
    $("candidates-button").setAttribute("aria-pressed", String(state.showCandidates));
    $("candidate-legend").classList.toggle("layer-hidden", !state.showCandidates);
    $("candidate-index").disabled = !count;
    $("candidate-locate").disabled = !count;
    $("candidate-index").max = String(count);
    $("candidate-index").placeholder = count ? t("编号 1—{count}", { count }) : t("本图暂无记录");
  }

  async function loadCandidates() {
    try {
      const response = await fetch("/candidates.json", { cache: "no-cache", signal: AbortSignal.timeout(10000) });
      if (!response.ok) throw new Error(t("点位文件不可用"));
      const data = await response.json();
      if (data.schema !== 1 || data.kind !== "historical_candidate" || !data.maps) throw new Error(t("点位文件格式无效"));
      for (const map of Object.values(data.maps)) {
        if (!Array.isArray(map.points) || map.points.length !== map.unique_count || map.points.some(item => !Number.isFinite(item.x) || !Number.isFinite(item.y))) throw new Error(t("点位坐标无效"));
      }
      state.candidates = data; state.candidateError = false;
    } catch (_) { state.candidateError = true; }
    renderCandidates(); requestPaint();
  }

  function setCandidatesVisible(value) {
    state.showCandidates = value;
    try { localStorage.setItem("redbag-show-candidates", String(value)); } catch (_) {}
    if (!value) { state.selectedCandidate = null; view.hover = null; $("marker-tooltip").hidden = true; }
    renderCandidates(); renderSelected(); requestPaint();
  }

  function selectCandidate(item, focus = false) {
    state.selected = null; state.selectedCandidate = item.id;
    if (!state.showCandidates) setCandidatesVisible(true);
    $("candidate-index").value = item.ordinal;
    if (focus) focusTarget(item);
    renderRecords(true); requestPaint();
  }

  function clearTarget() {
    state.selected = null; state.selectedCandidate = null;
    state.alertRecord = null; state.flashId = null; state.flashUntil = 0;
    activeAlert = null; view.hover = null;
    clearTimeout(alertTimer); alertTimer = null;
    $("alert-banner").hidden = true; $("alert-locate").hidden = true;
    $("marker-tooltip").hidden = true; $("candidate-index").value = "";
    // Keep observations and the alert sequence: polling must not restore a dismissed target.
    renderRecords(true); requestPaint();
  }

  function renderRecords(force = false) {
    const records = mapRecords().sort(newestFirst);
    const { other } = storageSummary();
    $("record-count").textContent = String(records.length);
    $("latest-button").disabled = records.length === 0;
    const shown = records.filter(inFilter);
    const key = JSON.stringify([state.selected, state.filter, shown.map(item => [item.id, displayStatus(item), item.x, item.y, latestRecordTime(item), item.opened_at, item.source, statusFor(item)]), state.flashId, other]);
    if (key === state.listKey && !force) { renderSelected(); return; }
    state.listKey = key;
    if (!shown.length) {
      const empty = safeElement("div", "empty-state");
      const offline = state.data?.mode === "offline";
      empty.append(safeElement("span", "empty-icon", "◇"), safeElement("strong", "", records.length ? t("此筛选下暂无记录") : offline || other ? t("本图暂无本机接收记录") : t("等待红色背包记录")), safeElement("p", "", records.length ? t("可以切换到“全部”查看其他状态。") : other ? t("其他地图仍有 {count} 条记录，可从地图列表查看。", { count: number(other) }) : offline ? t("当前仅浏览历史点位，没有启动实时接收。可点击地图上的蓝色菱形查看候选位置。") : t("正常跑图即可。收到位置后，会在这里和地图上显示。")));
      $("record-list").replaceChildren(empty);
    } else {
      $("record-list").replaceChildren(...shown.map(item => {
        const row = safeElement("button", `record-row${String(item.id) === String(state.selected) ? " selected" : ""}${String(item.id) === String(state.flashId) && now() < state.flashUntil ? " flash" : ""}`);
        row.type = "button"; row.setAttribute("aria-label", t("红色背包，{status}，X {x}，Y {y}", { status: statusFor(item), x: number(item.x), y: number(item.y) })); row.setAttribute("aria-pressed", String(String(item.id) === String(state.selected)));
        const top = safeElement("div", "record-row-top"); top.append(safeElement("span", `record-dot ${displayStatus(item)}`), safeElement("span", "record-row-title", t("红色背包")), safeElement("span", `record-status ${displayStatus(item)}`, statusFor(item)));
        const coords = safeElement("div", "record-row-coordinates"); for (const axis of ["x", "y"]) { const part = safeElement("span"); part.append(safeElement("b", "", axis.toUpperCase()), document.createTextNode(number(item[axis]))); coords.append(part); }
        const bottom = safeElement("div", "record-row-bottom"); bottom.append(safeElement("span", item.source === "history" ? "history-tag" : "", sourceFor(item)), safeElement("span", "", t("{time} 更新", { time: clock(latestRecordTime(item)) })));
        row.append(top, coords, bottom); row.addEventListener("click", () => selectRecord(item.id, true)); return row;
      }));
    }
    renderSelected();
  }

  function renderSelected() {
    const candidate = selectedCandidate(), item = selectedRecord() || candidate;
    $("clear-target").disabled = !item && state.alertRecord === null && state.flashId === null;
    $("selected-card").hidden = !item; if (!item) return;
    const distance = playerDistance(item), player = playerOnMap();
    $("selected-distance").textContent = distance !== null ? t("平面直线约 {distance} 米", { distance: number(distance) }) : player?.stale ? t("位置已过期，暂不计算距离") : t("等待本图新的角色参考位置以计算距离");
    $("selected-distance").classList.toggle("unavailable", distance === null);
    $("selected-refresh").hidden = !!candidate || !hasRefreshEstimate(item);
    if (candidate) {
      $("selected-name").textContent = t("隐藏背包候选 #{number}", { number: candidate.ordinal });
      $("selected-status").textContent = t("历史候选"); $("selected-status").className = "record-status candidate";
      $("selected-coordinates").textContent = `X ${number(candidate.x)}   Y ${number(candidate.y)}   ${t("Z 未记录")}`;
      $("selected-time").textContent = t("背包颜色未知 · 当前刷新状态未知");
      $("selected-source").textContent = t("A2Helper 0.1.25 · {count} 条原始记录。历史候选位置不触发刷新提醒。", { count: candidate.source_indices.length });
      return;
    }
    $("selected-name").textContent = t("红色背包");
    $("selected-status").textContent = statusFor(item); $("selected-status").className = `record-status ${displayStatus(item)}`;
    $("selected-coordinates").textContent = `X ${number(item.x)}   Y ${number(item.y)}   Z ${number(item.z)}`;
    $("selected-time").textContent = t("最新更新 {time}", { time: fullTime(latestRecordTime(item)) }) + `\n${t("首次发现 {time}", { time: fullTime(item.first_seen) })}` + (stamp(item.opened_at) ? `\n${t("已开启 {time}", { time: fullTime(item.opened_at) })}` : "");
    $("selected-source").textContent = `${sourceFor(item)} · ${t("实收状态：{status}。", { status: rawStatusFor(item) })}${item.status === "opened" ? t("此后是否刷新未知。") : item.status === "used" ? t("是否已开启待确认。") : t("当前是否仍可拾取未知。")}`;
    if (hasRefreshEstimate(item)) {
      const window = refreshWindow(item);
      $("selected-refresh").classList.toggle("soon", displayStatus(item) === "soon");
      $("refresh-countdown").textContent = refreshCountdown(item);
      $("refresh-soon-time").textContent = fullTime(window.startAt);
      $("refresh-expected-time").textContent = fullTime(window.expectedAt);
      $("refresh-end-time").textContent = fullTime(window.endAt);
    }
  }

  function asset(url) {
    if (assets.has(url)) return assets.get(url);
    const item = { image: new Image(), loaded: false, failed: false };
    assets.set(url, item); item.image.onload = () => { item.loaded = true; requestPaint(); }; item.image.onerror = () => { item.failed = true; requestPaint(); }; item.image.src = url; return item;
  }
  const toScreen = (x, y) => ({ x: (x - view.cx) * view.scale + view.width / 2, y: (y - view.cy) * view.scale + view.height / 2 });
  const toWorld = (x, y) => ({ x: (x - view.width / 2) / view.scale + view.cx, y: (y - view.height / 2) / view.scale + view.cy });
  function fitMap() { if (view.width <= 0 || view.height <= 35) { view.ready = false; return; } const map = currentMap(); view.cx = Number(map.x) + Number(map.w) / 2; view.cy = Number(map.y) + Number(map.h) / 2; view.fit = Math.min(view.width / Number(map.w), (view.height - 35) / Number(map.h)) * .98; view.scale = view.fit; view.ready = true; requestPaint(); }
  function resize() { const rect = stage.getBoundingClientRect(); const oldFit = view.fit, wasFit = !view.ready || Math.abs(view.scale - oldFit) < oldFit * .025; view.width = rect.width; view.height = rect.height; view.dpr = Math.min(window.devicePixelRatio || 1, 2); canvas.width = Math.max(1, Math.round(view.width * view.dpr)); canvas.height = Math.max(1, Math.round(view.height * view.dpr)); if (wasFit) fitMap(); else { const map = currentMap(); view.fit = Math.min(view.width / Number(map.w), (view.height - 35) / Number(map.h)) * .98; requestPaint(); } followPlayerPosition(true); }
  function zoom(factor, sx = view.width / 2, sy = view.height / 2) { if (state.followPlayer && playerOnMap(true)) { sx = view.width / 2; sy = view.height / 2; } const before = toWorld(sx, sy); view.scale = Math.min(view.fit * 100, Math.max(view.fit * .45, view.scale * factor)); const after = toWorld(sx, sy); view.cx += before.x - after.x; view.cy += before.y - after.y; followPlayerPosition(true); requestPaint(); }
  function focusTarget(item, automatic = false) {
    if (!item || String(item.map_id ?? state.mapId) !== String(state.mapId) || ![item.x, item.y].every(value => Number.isFinite(Number(value)))) return false;
    if (automatic && view.pointer) return false;
    if (!view.ready) fitMap();
    if (!view.ready) return false;
    // Keep a closer user view; locating a bag must never zoom the map out.
    const detailScale = Math.min(view.fit * 100, Math.max(view.fit, Math.min(view.width, view.height - 70) / 24000));
    view.scale = Math.max(view.scale, detailScale);
    if (state.followPlayer && playerOnMap()) {
      // An expired reference keeps the last center, without asserting a live position.
      followPlayerPosition(true);
    } else {
      view.cx = Number(item.x); view.cy = Number(item.y);
    }
    requestPaint();
    return true;
  }
  function requestPaint() { if (!repaintPending) { repaintPending = true; requestAnimationFrame(() => { repaintPending = false; paint(); }); } }

  function gridStep() { const needed = 110 / view.scale, magnitude = Math.pow(10, Math.floor(Math.log10(needed))); for (const multiplier of [1, 2, 5, 10]) if (magnitude * multiplier >= needed) return magnitude * multiplier; return magnitude * 10; }
  function drawGrid() {
    const step = gridStep(), corner = toWorld(0, 0), far = toWorld(view.width, view.height), startX = Math.ceil(corner.x / step) * step, startY = Math.ceil(corner.y / step) * step;
    ctx.lineWidth = 1; ctx.font = "10px Consolas, monospace"; ctx.textBaseline = "top";
    for (let x = startX; x <= far.x; x += step) { const px = Math.round(toScreen(x, 0).x) + .5; ctx.strokeStyle = "rgba(71, 59, 39, .20)"; ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, view.height - 34); ctx.stroke(); if (px > 10 && px < view.width - 65) { ctx.fillStyle = "rgba(248, 241, 225, .82)"; const label = `X ${Math.round(x)}`, width = ctx.measureText(label).width; ctx.fillRect(px + 4, view.height - 54, width + 6, 16); ctx.fillStyle = "#62533d"; ctx.fillText(label, px + 7, view.height - 51); } }
    for (let y = startY; y <= far.y; y += step) { const py = Math.round(toScreen(0, y).y) + .5; ctx.strokeStyle = "rgba(71, 59, 39, .20)"; ctx.beginPath(); ctx.moveTo(0, py); ctx.lineTo(view.width, py); ctx.stroke(); if (py > 74 && py < view.height - 76) { const label = `Y ${Math.round(y)}`, width = ctx.measureText(label).width; ctx.fillStyle = "rgba(248, 241, 225, .82)"; ctx.fillRect(view.width - width - 14, py + 4, width + 9, 16); ctx.fillStyle = "#62533d"; ctx.fillText(label, view.width - width - 9, py + 7); } }
  }

  function drawMarker(item, index) {
    const point = toScreen(Number(item.x), Number(item.y)); if (point.x < -35 || point.y < -35 || point.x > view.width + 35 || point.y > view.height + 35) return;
    const selected = String(item.id) === String(state.selected), hovered = String(item.id) === String(view.hover), flash = String(item.id) === String(state.flashId) && now() < state.flashUntil, color = colorFor(item), size = selected || hovered ? 10 : 8;
    ctx.save(); ctx.translate(point.x, point.y);
    if (flash) { const radius = 17 + ((now() % 1.8) / 1.8) * 22; ctx.globalAlpha = .55 * (1 - (radius - 17) / 22); ctx.strokeStyle = color; ctx.lineWidth = 3; ctx.beginPath(); ctx.arc(0, 0, radius, 0, Math.PI * 2); ctx.stroke(); ctx.globalAlpha = 1; requestPaint(); }
    if (selected) { ctx.beginPath(); ctx.arc(0, 0, 19, 0, Math.PI * 2); ctx.fillStyle = "rgba(255, 247, 222, .70)"; ctx.fill(); ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.stroke(); ctx.strokeStyle = color; ctx.beginPath(); ctx.moveTo(-26, 0); ctx.lineTo(-20, 0); ctx.moveTo(20, 0); ctx.lineTo(26, 0); ctx.moveTo(0, -26); ctx.lineTo(0, -20); ctx.moveTo(0, 20); ctx.lineTo(0, 26); ctx.stroke(); }
    ctx.shadowColor = "rgba(28, 25, 20, .38)"; ctx.shadowBlur = 6; ctx.shadowOffsetY = 2; ctx.beginPath(); ctx.arc(0, 0, size + 2, 0, Math.PI * 2); ctx.fillStyle = "#fff3db"; ctx.fill(); ctx.shadowColor = "transparent"; ctx.shadowBlur = 0; ctx.shadowOffsetY = 0; ctx.beginPath(); ctx.arc(0, 0, size, 0, Math.PI * 2); ctx.fillStyle = color; ctx.fill();
    ctx.strokeStyle = "#fff8ec"; ctx.lineWidth = 1.6; ctx.lineCap = "round"; ctx.lineJoin = "round";
    if (displayStatus(item) === "soon") { ctx.beginPath(); ctx.arc(0, 0, 4.3, 0, Math.PI * 2); ctx.moveTo(0, -2.7); ctx.lineTo(0, .3); ctx.lineTo(2.4, 1.6); ctx.stroke(); }
    else if (displayStatus(item) === "opened") { ctx.beginPath(); ctx.moveTo(-3.7, .2); ctx.lineTo(-.7, 3); ctx.lineTo(4.5, -3); ctx.stroke(); }
    else if (displayStatus(item) === "unknown") { ctx.fillStyle = "#fff8ec"; ctx.font = "bold 11px Segoe UI, sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText("?", 0, .3); }
    else { ctx.beginPath(); ctx.rect(-3.2, -2.5, 6.4, 6); ctx.moveTo(-1.5, -2.5); ctx.lineTo(-1.5, -4.4); ctx.lineTo(1.5, -4.4); ctx.lineTo(1.5, -2.5); ctx.stroke(); }
    if (selected || hovered || mapRecords().length === 1) { ctx.font = "11px Microsoft YaHei UI, sans-serif"; ctx.textAlign = "left"; ctx.textBaseline = "middle"; const label = t("红色背包 · {status}", { status: statusFor(item) }), width = ctx.measureText(label).width + 18; const left = point.x + width + 26 > view.width ? -width - 17 : 17; ctx.fillStyle = "rgba(248, 243, 231, .94)"; ctx.strokeStyle = "rgba(99, 78, 46, .35)"; ctx.lineWidth = 1; ctx.beginPath(); ctx.roundRect(left, -13, width, 26, 4); ctx.fill(); ctx.stroke(); ctx.fillStyle = "#5c4837"; ctx.fillText(label, left + 9, 0); }
    ctx.restore();
  }

  function drawScale() {
    const rawMeters = 100 / view.scale / 100, magnitude = Math.pow(10, Math.floor(Math.log10(rawMeters))); let meters = magnitude; for (const k of [1, 2, 5]) if (magnitude * k <= rawMeters) meters = magnitude * k;
    const width = meters * 100 * view.scale, x = 72, y = view.height - 66; if (width < 20 || view.width < 300) return;
    ctx.strokeStyle = "#f8f0dc"; ctx.lineWidth = 5; ctx.beginPath(); ctx.moveTo(x, y - 5); ctx.lineTo(x, y); ctx.lineTo(x + width, y); ctx.lineTo(x + width, y - 5); ctx.stroke(); ctx.strokeStyle = "#6a5c45"; ctx.lineWidth = 1.5; ctx.stroke(); ctx.textAlign = "center"; ctx.textBaseline = "bottom"; ctx.font = "10px Microsoft YaHei UI, sans-serif"; ctx.fillStyle = "#f8f0dc"; ctx.lineWidth = 3; ctx.strokeStyle = "#f8f0dc"; const label = meters >= 1000 ? t("{count} 千米", { count: meters / 1000 }) : t("{count} 米", { count: meters }); ctx.strokeText(label, x + width / 2, y - 7); ctx.fillStyle = "#66563c"; ctx.fillText(label, x + width / 2, y - 7); ctx.textAlign = "left";
  }

  function drawCandidates() {
    if (!state.showCandidates) return;
    const level = view.scale / view.fit, size = level < 2 ? 2.6 : level < 6 ? 3.7 : 5;
    ctx.save();
    for (const item of mapCandidates()) {
      const point = toScreen(item.x, item.y);
      if (point.x < -25 || point.y < -25 || point.x > view.width + 25 || point.y > view.height + 25) continue;
      const active = item.id === state.selectedCandidate || item.id === view.hover;
      const r = active ? 7 : size;
      ctx.beginPath(); ctx.moveTo(point.x, point.y - r); ctx.lineTo(point.x + r, point.y); ctx.lineTo(point.x, point.y + r); ctx.lineTo(point.x - r, point.y); ctx.closePath();
      ctx.fillStyle = active ? "#e1f4ff" : "rgba(241,250,253,.42)"; ctx.fill(); ctx.strokeStyle = active ? "#125576" : "rgba(29,103,130,.8)"; ctx.lineWidth = active ? 2 : 1.2; ctx.stroke();
      if (active) {
        ctx.beginPath(); ctx.arc(point.x, point.y, 16, 0, Math.PI * 2); ctx.strokeStyle = "#286f8f"; ctx.lineWidth = 1.3; ctx.stroke();
        ctx.font = "11px Microsoft YaHei UI, sans-serif"; ctx.textBaseline = "middle";
        const label = t("历史候选 #{number}", { number: item.ordinal }), width = ctx.measureText(label).width + 16;
        const left = point.x + width + 24 > view.width ? point.x - width - 19 : point.x + 19;
        ctx.fillStyle = "#edf8fff2"; ctx.beginPath(); ctx.roundRect(left, point.y - 12, width, 24, 4); ctx.fill(); ctx.fillStyle = "#205f7b"; ctx.fillText(label, left + 8, point.y);
      }
    }
    ctx.restore();
  }

  function drawPlayer() {
    const player = playerOnMap();
    if (!player) return;
    const point = toScreen(player.x, player.y), item = selectedRecord() || selectedCandidate();
    ctx.save();
    if (playerDistance(item) !== null) {
      const target = toScreen(item.x, item.y);
      ctx.beginPath(); ctx.moveTo(point.x, point.y); ctx.lineTo(target.x, target.y);
      ctx.setLineDash([6, 6]); ctx.strokeStyle = "#1c799aaa"; ctx.lineWidth = 2; ctx.stroke();
    }
    ctx.setLineDash(player.stale ? [3, 3] : []);
    ctx.beginPath(); ctx.arc(point.x, point.y, 12, 0, Math.PI * 2);
    ctx.fillStyle = player.stale ? "#e2e9eb77" : "#53c5ed44"; ctx.fill();
    ctx.strokeStyle = player.stale ? "#647a86" : "#167da1"; ctx.lineWidth = 2; ctx.stroke();
    ctx.setLineDash([]); ctx.beginPath(); ctx.arc(point.x, point.y, 6, 0, Math.PI * 2);
    ctx.fillStyle = player.stale ? "#82929a" : "#148db6"; ctx.fill(); ctx.strokeStyle = "#fff"; ctx.stroke();
    const label = player.stale ? t("最近位置（过期）") : t("角色参考位置");
    ctx.font = '11px "Segoe UI", sans-serif'; ctx.textBaseline = "middle";
    const width = ctx.measureText(label).width;
    ctx.fillStyle = "#f8faf5e8"; ctx.fillRect(point.x + 17, point.y - 11, width + 12, 22);
    ctx.fillStyle = player.stale ? "#63737d" : "#155970"; ctx.fillText(label, point.x + 23, point.y);
    ctx.restore();
  }

  function targetEdge(item = selectedRecord() || selectedCandidate()) {
    if (!item || String(item.map_id ?? state.mapId) !== String(state.mapId) || ![item.x, item.y].every(value => Number.isFinite(Number(value))) || view.width < 120 || view.height < 160) return null;
    const point = toScreen(Number(item.x), Number(item.y));
    if (point.x >= 0 && point.x <= view.width && point.y >= 0 && point.y <= view.height - 34) return null;
    const cx = view.width / 2, cy = view.height / 2, dx = point.x - cx, dy = point.y - cy;
    const left = 30, right = view.width - 30, top = Math.min(cy - 22, $("alert-banner").hidden ? 90 : 165), bottom = Math.max(cy + 22, view.height - 90);
    const ratio = Math.min(dx > 0 ? (right - cx) / dx : dx < 0 ? (left - cx) / dx : Infinity, dy > 0 ? (bottom - cy) / dy : dy < 0 ? (top - cy) / dy : Infinity);
    return { item, x: cx + dx * ratio, y: cy + dy * ratio, angle: Math.atan2(dy, dx) };
  }

  function drawTargetEdge() {
    const edge = targetEdge(); if (!edge) return;
    const { item, x, y, angle } = edge, color = item.source_indices ? "#826943" : colorFor(item);
    ctx.save();
    ctx.translate(x, y); ctx.rotate(angle); ctx.beginPath(); ctx.moveTo(15, 0); ctx.lineTo(-9, -10); ctx.lineTo(-5, 0); ctx.lineTo(-9, 10); ctx.closePath();
    ctx.shadowColor = "#231b1255"; ctx.shadowBlur = 6; ctx.fillStyle = color; ctx.fill(); ctx.shadowBlur = 0; ctx.strokeStyle = "#fff5df"; ctx.lineWidth = 2; ctx.stroke(); ctx.restore();
    ctx.save(); ctx.font = 'bold 12px "Microsoft YaHei UI", sans-serif'; ctx.textBaseline = "middle";
    const title = item.source_indices ? t("所选候选点在画面外") : t("红色背包在画面外");
    const distance = playerDistance(item);
    const detail = distance === null ? t("箭头仅表示当前地图画面方向") : t("平面直线约 {distance} 米", { distance: number(distance) });
    const width = Math.min(view.width - 20, Math.max(ctx.measureText(title).width, ctx.measureText(detail).width) + 24), height = 47;
    const labelX = Math.max(10, Math.min(view.width - width - 10, x - width / 2 - Math.cos(angle) * (width / 2 + 25)));
    const labelY = Math.max(10, Math.min(view.height - height - 42, y - height / 2 - Math.sin(angle) * 34));
    ctx.fillStyle = "#fff7e9f5"; ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.roundRect(labelX, labelY, width, height, 6); ctx.fill(); ctx.stroke();
    ctx.fillStyle = color; ctx.fillText(title, labelX + 12, labelY + 15, width - 24); ctx.font = '10px "Microsoft YaHei UI", sans-serif'; ctx.fillStyle = "#665847"; ctx.fillText(detail, labelX + 12, labelY + 33, width - 24); ctx.restore();
  }

  function paint() {
    if (!view.width || !view.height) return;
    ctx.setTransform(view.dpr, 0, 0, view.dpr, 0, 0); ctx.clearRect(0, 0, view.width, view.height); ctx.fillStyle = "#cfc3ae"; ctx.fillRect(0, 0, view.width, view.height);
    const map = currentMap(), origin = toScreen(Number(map.x), Number(map.y)), w = Number(map.w) * view.scale, h = Number(map.h) * view.scale;
    const coordinateGrid = map.bounds_kind === "coordinate_grid";
    const base = coordinateGrid ? {loaded: false, failed: false} : asset(`/maps/${encodeURIComponent(map.id)}.webp`);
    if (base.loaded) { ctx.save(); ctx.shadowColor = "#55432644"; ctx.shadowBlur = 20; ctx.fillStyle = "#d8cdb7"; ctx.fillRect(origin.x, origin.y, w, h); ctx.shadowBlur = 0; ctx.drawImage(base.image, origin.x, origin.y, w, h); ctx.restore(); }
    $("map-empty").hidden = !(coordinateGrid || base.failed);
    if (view.scale / view.fit > 1.15 && Array.isArray(map.tiles) && map.tiles.length >= 2 && base.loaded) {
      const cols = Number(map.tiles[0]), rows = Number(map.tiles[1]), tw = Number(map.w) / cols, th = Number(map.h) / rows, near = toWorld(0, 0), far = toWorld(view.width, view.height);
      const c0 = Math.max(0, Math.floor((near.x - Number(map.x)) / tw)), c1 = Math.min(cols - 1, Math.floor((far.x - Number(map.x)) / tw)), r0 = Math.max(0, Math.floor((near.y - Number(map.y)) / th)), r1 = Math.min(rows - 1, Math.floor((far.y - Number(map.y)) / th));
      for (let row = r0; row <= r1; row++) for (let col = c0; col <= c1; col++) { const tile = asset(`/maps/${encodeURIComponent(map.id)}/${col}_${row}.webp`); if (tile.loaded) { const point = toScreen(Number(map.x) + col * tw, Number(map.y) + row * th); ctx.drawImage(tile.image, point.x, point.y, tw * view.scale + .2, th * view.scale + .2); } }
    }
    if (state.grid || coordinateGrid) drawGrid();
    drawCandidates();
    const records = visibleRecords(); records.sort((a, b) => (String(a.id) === String(state.selected) ? 1 : 0) - (String(b.id) === String(state.selected) ? 1 : 0)); records.forEach(drawMarker);
    drawPlayer(); drawScale(); drawTargetEdge(); $("zoom-level").textContent = `${(view.scale / view.fit).toFixed(view.scale / view.fit < 10 ? 1 : 0)}×`;
  }

  async function selectRecord(id, focus = false) {
    const item = currentRecordFor(id); if (!item) return;
    const session = state.session;
    if (String(item.map_id) !== String(state.mapId)) { await control("map", { map_id: Number(item.map_id) }); if (state.session !== session || String(item.map_id) !== String(state.mapId)) return; }
    state.selected = item.id; state.selectedCandidate = null;
    if (focus) focusTarget(item);
    renderRecords(true); requestPaint();
  }

  function hitMarker(x, y) {
    const live = visibleRecords().map(item => { const point = toScreen(Number(item.x), Number(item.y)); return { item, distance: Math.hypot(x - point.x, y - point.y) }; }).filter(hit => hit.distance < 15).sort((a, b) => a.distance - b.distance)[0]?.item;
    if (live) return live;
    if (!state.showCandidates) return null;
    return mapCandidates().map(item => { const point = toScreen(item.x, item.y); return { item, distance: Math.hypot(x - point.x, y - point.y) }; }).filter(hit => hit.distance < 10).sort((a, b) => a.distance - b.distance)[0]?.item || null;
  }
  function pointerPosition(event) { const rect = canvas.getBoundingClientRect(); return { x: event.clientX - rect.left, y: event.clientY - rect.top }; }
  canvas.addEventListener("pointerdown", event => { if (event.button !== 0) return; const point = pointerPosition(event); view.pointer = { id: event.pointerId, x: point.x, y: point.y, startX: point.x, startY: point.y, cx: view.cx, cy: view.cy, moved: false }; canvas.setPointerCapture(event.pointerId); canvas.classList.add("dragging"); $("marker-tooltip").hidden = true; });
  canvas.addEventListener("pointermove", event => { const point = pointerPosition(event), world = toWorld(point.x, point.y); $("cursor-coordinates").textContent = `X ${number(world.x)}   Y ${number(world.y)}`; if (view.pointer) { const pointer = view.pointer; if (Math.hypot(point.x - pointer.startX, point.y - pointer.startY) > 4) { pointer.moved = true; if (state.followPlayer) setFollowPlayer(false); } view.cx = pointer.cx - (point.x - pointer.startX) / view.scale; view.cy = pointer.cy - (point.y - pointer.startY) / view.scale; requestPaint(); return; } const hit = hitMarker(point.x, point.y); view.hover = hit?.id || null; canvas.classList.toggle("over-marker", !!hit); if (hit) { const tooltip = $("marker-tooltip"); tooltip.textContent = markerTooltip(hit); tooltip.hidden = false; tooltip.style.left = `${Math.max(8, Math.min(view.width - 230, point.x + 18))}px`; tooltip.style.top = `${Math.max(8, Math.min(view.height - 120, point.y + 18))}px`; } else $("marker-tooltip").hidden = true; requestPaint(); });
  function endPointer(event) { if (!view.pointer || view.pointer.id !== event.pointerId) return; const pointer = view.pointer, point = pointerPosition(event); view.pointer = null; canvas.classList.remove("dragging"); if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId); if (!pointer.moved && event.type !== "pointercancel") { const hit = hitMarker(point.x, point.y); if (hit) { if (hit.source_indices) selectCandidate(hit); else selectRecord(hit.id, false); } } }
  canvas.addEventListener("pointerup", endPointer); canvas.addEventListener("pointercancel", endPointer); canvas.addEventListener("pointerleave", () => { if (!view.pointer) { view.hover = null; $("marker-tooltip").hidden = true; requestPaint(); } });
  canvas.addEventListener("wheel", event => { event.preventDefault(); const point = pointerPosition(event); zoom(Math.exp(-Math.max(-150, Math.min(150, event.deltaY)) * .002), point.x, point.y); }, { passive: false });
  canvas.addEventListener("dblclick", event => { const point = pointerPosition(event); zoom(1.7, point.x, point.y); });
  canvas.addEventListener("keydown", event => { if (["0", "Home", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) setFollowPlayer(false); if (["+", "="].includes(event.key)) zoom(1.4); else if (event.key === "-") zoom(1 / 1.4); else if (["0", "Home"].includes(event.key)) fitMap(); else if (event.key === "ArrowLeft") view.cx -= 80 / view.scale; else if (event.key === "ArrowRight") view.cx += 80 / view.scale; else if (event.key === "ArrowUp") view.cy -= 80 / view.scale; else if (event.key === "ArrowDown") view.cy += 80 / view.scale; else return; event.preventDefault(); requestPaint(); });

  function notifyPermissionLabel() { const offline = !!state.data && !state.connected, native = !offline && state.data?.notification?.available === true, enabled = !offline && state.notify && "Notification" in window && Notification.permission === "granted", button = $("notification-button"); button.disabled = native || offline; button.title = offline ? t("本地工具已断开连接，当前无法确认提醒服务状态。") : native ? t("工具通过 Windows 系统通知提醒你，页面隐藏时也可提醒，无需再开启浏览器通知。") : t("系统通知不可用时，可选择使用当前浏览器的通知。"); button.classList.toggle("enabled", native || enabled); button.replaceChildren(document.createTextNode(offline ? t("通知连接已断开") : native ? t("系统通知已就绪") : enabled ? t("浏览器通知已开启") : t("开启浏览器通知")), safeElement("span", "", native || enabled ? "✓" : "↗")); }
  function renderAlert(alert) {
    const item = state.records.find(record => String(record.id) === String(alert.record_id)); state.alertRecord = item?.id || null;
    const replay = state.data?.mode === "replay" || item?.source === "replay";
    $("alert-title").textContent = replay ? (item ? t("历史回放 · 红色背包记录") : t("历史回放 · 提醒测试")) : (item ? t("收到新的红色背包记录") : t("提醒测试"));
    const map = item && (state.maps.find(candidate => String(candidate.id) === String(item.map_id)) || { id: item.map_id });
    const text = item ? `${mapName(map)} · X ${number(item.x)} · Y ${number(item.y)} · ${clock(alert.at)}` : alert.test ? t("这是提醒测试，不代表发现背包。实际收到新记录后，会显示地图与坐标。") : alert.text ? localizedDetail(alert.text) : t("声音和地图提醒已触发。此提示不代表发现背包。");
    $("alert-text").textContent = `${replay ? t("历史采样，非当前游戏状态。") + " " : ""}${text}`;
    $("alert-locate").hidden = !item;
    return { item, replay };
  }
  function showAlert(alert) {
    activeAlert = alert;
    const { item, replay } = renderAlert(alert);
    $("alert-banner").hidden = false;
    if (item) {
      state.flashId = item.id; state.flashUntil = now() + 18;
      if (!replay && item.source === "live" && String(item.map_id) === String(state.mapId)) {
        const target = currentRecordFor(item.id);
        if (target) { state.selected = target.id; state.selectedCandidate = null; focusTarget(target, true); }
      }
      renderRecords(true); requestPaint();
    }
    clearTimeout(alertTimer); alertTimer = setTimeout(() => { $("alert-banner").hidden = true; state.flashId = null; renderRecords(true); requestPaint(); }, 30000);
    if (!replay && state.data?.notification?.available !== true && state.notify && "Notification" in window && Notification.permission === "granted") { try { const notification = new Notification(item ? t("AION2 · 收到红色背包记录") : t("AION2 · 测试提醒"), { body: $("alert-text").textContent, tag: `redbag-${alert.seq}`, silent: true }); notification.onclick = () => { window.focus(); if (item) selectRecord(item.id, true); notification.close(); }; } catch (_) {} }
  }

  function acceptData(data) {
    const previousMap = state.mapId, changedSession = state.session !== data.session_id;
    state.data = data; state.connected = true; state.maps = Array.isArray(data.maps) ? data.maps : []; state.records = recordsWithRefreshEstimates(Array.isArray(data.records) ? data.records : []); state.data.records = state.records; state.session = data.session_id;
    state.mapId = data.selected_map_id ?? state.maps[0]?.id ?? 1110;
    const changedMap = String(previousMap) !== String(state.mapId);
    if (changedSession || changedMap) {
      state.selected = null; state.selectedCandidate = null; state.playerFollowKey = null;
      state.alertRecord = null; state.flashId = null; state.flashUntil = 0; activeAlert = null;
      clearTimeout(alertTimer); $("alert-banner").hidden = true; $("candidate-index").value = "";
    } else if (state.selected !== null) state.selected = currentRecordFor(state.selected)?.id ?? null;
    if (changedMap) { view.ready = false; fitMap(); }
    const seq = Number(data.alert_seq || 0);
    if (state.lastAlertSeq === null || changedSession) state.lastAlertSeq = seq;
    else if (seq > state.lastAlertSeq) { state.lastAlertSeq = seq; if (data.latest_alert) showAlert(data.latest_alert); }
    followPlayerPosition(); renderPlayer(); renderConnection(); renderMaps(); renderRecords(); requestPaint(); syncLanguage();
  }

  async function syncLanguage() {
    if (!state.connected || !state.data?.control_token || !Object.prototype.hasOwnProperty.call(state.data, "language")) return;
    const language = locale(), key = `${state.session}:${language}`;
    if (languageSyncKey === key) return;
    languageSyncKey = key;
    if (state.data.language === language) return;
    try {
      const response = await fetch("/api/control", { method: "POST", headers: { "Content-Type": "application/json", "X-Redbag-Key": state.data.control_token }, body: JSON.stringify({ action: "language", language }), signal: AbortSignal.timeout(5000) });
      if (!response.ok) throw new Error("language sync failed");
      const result = await response.json();
      if (result.ok === false) throw new Error("language sync failed");
    } catch (_) { if (locale() === language) toast(t("界面语言已切换；系统通知语言暂未同步。")); }
  }

  function changeLanguage(code) {
    if (!window.I18N.setLanguage(code)) return;
    window.I18N.apply(); $("language-select").value = locale();
    state.mapKey = ""; state.listKey = "";
    renderMaps(); renderRecords(true); renderClock(); renderConnection();
    if (activeAlert && !$("alert-banner").hidden) renderAlert(activeAlert);
    if (!$("toast").hidden) $("toast").textContent = localizedDetail($("toast").textContent);
    requestPaint(); syncLanguage();
  }

  async function poll() { clearTimeout(pollTimer); try { const response = await fetch("/api/state", { cache: "no-store", signal: AbortSignal.timeout(3500) }); if (!response.ok) throw new Error(`HTTP ${response.status}`); const data = await response.json(); acceptData(data); } catch (_) { state.connected = false; renderConnection(); renderPlayer(); renderSelected(); requestPaint(); } finally { pollTimer = setTimeout(poll, state.connected ? 750 : 1800); } }
  async function control(action, extra = {}) {
    if (!state.connected || state.busy || !state.data?.control_token) { toast(t("工具尚未连接，请等待本地连接恢复。")); return false; }
    state.busy = true; renderConnection();
    try { const response = await fetch("/api/control", { method: "POST", headers: { "Content-Type": "application/json", "X-Redbag-Key": state.data.control_token }, body: JSON.stringify({ action, ...extra }), signal: AbortSignal.timeout(5000) }); const result = await response.json(); if (!response.ok || result.ok === false) throw new Error(result.error || t("操作未完成")); if (result.maps && result.capture) acceptData(result); else { const fresh = await fetch("/api/state", { cache: "no-store", signal: AbortSignal.timeout(3500) }); if (fresh.ok) acceptData(await fresh.json()); } return true; } catch (error) { if (action === "shutdown") { toast(t("已发送退出请求。连接关闭后，接收和提醒会停止。")); state.connected = false; } else toast(t("操作未完成：{reason}", { reason: localizedDetail(error.message) })); return false; } finally { state.busy = false; renderConnection(); }
  }

  $("zoom-in").addEventListener("click", () => zoom(1.5)); $("zoom-out").addEventListener("click", () => zoom(1 / 1.5)); $("fit-button").addEventListener("click", () => { setFollowPlayer(false); fitMap(); });
  $("follow-player").addEventListener("click", () => setFollowPlayer(!state.followPlayer));
  $("open-data-folder").addEventListener("click", () => { if (state.data?.storage?.directory) control("open_data_folder"); });
  $("export-history").addEventListener("click", event => { if (!state.data?.storage?.directory || !state.connected || state.busy) { event.preventDefault(); toast(t("工具尚未连接，请等待本地连接恢复。")); } });
  $("candidates-button").addEventListener("click", () => { if (state.candidateError) loadCandidates(); else setCandidatesVisible(!state.showCandidates); });
  $("candidate-index-form").addEventListener("submit", event => { event.preventDefault(); const ordinal = Number($("candidate-index").value); if (!Number.isInteger(ordinal) || ordinal < 1 || ordinal > mapCandidates().length) { toast(t("请输入当前地图中有效的点位编号。")); return; } selectCandidate(mapCandidates()[ordinal - 1], true); });
  $("grid-button").addEventListener("click", () => { state.grid = !state.grid; $("grid-button").classList.toggle("active", state.grid); $("grid-button").setAttribute("aria-pressed", String(state.grid)); requestPaint(); });
  $("map-select").addEventListener("change", async event => { setFollowPlayer(false); const previous = state.mapId; if (!(await control("map", { map_id: Number(event.target.value) }))) $("map-select").value = String(previous); });
  for (const tab of document.querySelectorAll(".filter-tab")) tab.addEventListener("click", () => { state.filter = tab.dataset.filter; for (const other of document.querySelectorAll(".filter-tab")) { other.classList.toggle("active", other === tab); other.setAttribute("aria-pressed", String(other === tab)); } renderRecords(true); requestPaint(); });
  $("latest-button").addEventListener("click", () => { const item = mapRecords().sort(newestFirst)[0]; if (item) { if (!inFilter(item)) { document.querySelector('[data-filter="all"]').click(); } selectRecord(item.id, true); } });
  $("clear-selection").addEventListener("click", clearTarget);
  $("clear-target").addEventListener("click", clearTarget);
  $("copy-coordinates").addEventListener("click", async () => {
    const candidate = selectedCandidate(), item = selectedRecord() || candidate; if (!item) return;
    const window = !candidate ? refreshWindow(item) : null;
    const lines = candidate ? [
      `${mapName(currentMap())} · ${t("历史隐藏背包候选 #{number}", { number: candidate.ordinal })}`,
      `X ${Math.round(item.x)} · Y ${Math.round(item.y)} · ${t("Z 未记录")}`,
      t("A2Helper 0.1.25 · 颜色未知 · 当前刷新状态未知")
    ] : [
      `${mapName(currentMap())} · ${t("红色背包")}`,
      `X ${Math.round(item.x)} · Y ${Math.round(item.y)} · Z ${Math.round(item.z)}`,
      `${statusFor(item)} · ${t("实收状态：{status}。", { status: rawStatusFor(item) })}`
    ];
    if (!candidate && stamp(item.opened_at)) lines.push(t("已开启 {time}", { time: fullTime(item.opened_at) }));
    if (window) lines.push(
      t("最新更新 {time}", { time: fullTime(latestRecordTime(item)) }),
      t("首次发现 {time}", { time: fullTime(item.first_seen) }),
      t("预警开始 {start} · 预计刷新 {expected} · 提示结束 {end}", { start: fullTime(window.startAt), expected: fullTime(window.expectedAt), end: fullTime(window.endAt) }),
      t("以最新更新时间为起点，按每小时估算，仅在预计刷新前后 5 分钟提示，尚未证实已刷新。")
    );
    lines.push(t("北京时间 UTC+8"));
    try { await navigator.clipboard.writeText(lines.join("\n")); toast(t("坐标已复制。")); } catch (_) { toast(t("浏览器未允许复制，可直接选取坐标文本。")); }
  });
    $("pause-button").addEventListener("click", () => control(state.data?.paused ? "resume" : "pause")); $("sound-button").addEventListener("click", () => control("sound", { enabled: state.data?.sound === false })); $("test-alert-button").addEventListener("click", async () => { if (await control("test_alert")) showAlert({ seq: `test-${Date.now()}`, at: now(), test: true }); });
  $("stop-button").addEventListener("click", async () => { if (await control("shutdown")) { toast(t("工具正在退出，接收和提醒将停止。")); state.connected = false; renderConnection(); } });
  $("notification-button").addEventListener("click", async () => { if (state.data?.notification?.available === true) return; if (!("Notification" in window)) { toast(t("当前浏览器不支持桌面通知；地图提示和声音仍可使用。")); return; } if (state.notify && Notification.permission === "granted") state.notify = false; else { let permission = Notification.permission; if (permission === "default") permission = await Notification.requestPermission(); state.notify = permission === "granted"; if (!state.notify) toast(t("桌面通知未获允许；地图提示和声音仍可使用。")); } try { localStorage.setItem("redbag-desktop-notifications", String(state.notify)); } catch (_) {} notifyPermissionLabel(); });
  $("alert-locate").addEventListener("click", () => { if (state.alertRecord) { if (state.filter !== "all") document.querySelector('[data-filter="all"]').click(); selectRecord(state.alertRecord, true); } }); $("alert-dismiss").addEventListener("click", () => { $("alert-banner").hidden = true; });

  $("language-select").value = locale(); $("language-select").addEventListener("change", event => changeLanguage(event.target.value));
  window.I18N.apply();
  new ResizeObserver(resize).observe(stage); notifyPermissionLabel(); renderClock(); setInterval(renderClock, 1000); loadCandidates(); poll();
})();
