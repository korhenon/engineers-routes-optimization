// Рабочее место диспетчера: участок → базовый и оптимизированный план → карта, списки, карточки, метрики;
// событие → перепланирование → diff‑режим (старые маршруты пунктиром, изменённые заявки подсвечены).
// Линии маршрутов — по дорогам (/geometry, догружаются после плана; до этого — прямые). Загрузка своих
// файлов — /api/upload.
// Время из API приходит в минутах от 00:00, здесь форматируется в ЧЧ:ММ.

const EMERGENCY = 3;
const COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#42d4f4", "#f032e6", "#9a6324",
                "#469990", "#800000", "#808000", "#000075", "#bfef45", "#dcbeff", "#fabed4"];

const state = {
  area: null, data: null,
  plans: {},        // алгоритм -> текущий план (после событий — последний перепланированный)
  byId: {},         // все планы сессии по id: нужны родители для diff и отката
  diffs: {},        // id плана -> diff относительно родителя (из /api/replan)
  explain: {},      // "plan/request" -> строки объяснения
  geometry: {},     // id плана -> {engineer_id: [[lat, lon], …]} по дорогам (null — запрос в полёте)
  algo: "optimized", selected: null,
  panel: null,      // null | "event" | "upload"
  draft: null,      // черновик формы события
  showDiff: true, pickPoint: false, busy: false,
};
const $ = (id) => document.getElementById(id);

const map = L.map("map", { zoomControl: false }).setView([55.75, 37.62], 10);
L.control.zoom({ position: "topright" }).addTo(map);
// Обычные тайлы OSM (без ключа), приглушённые CSS-фильтром (.base-tiles), чтобы маршруты читались поверх;
// в тёмной теме фильтр их инвертирует.
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19, className: "base-tiles", attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
}).addTo(map);
const layer = L.layerGroup().addTo(map);

const legend = L.control({ position: "bottomleft" });
legend.onAdd = () => {
  const div = L.DomUtil.create("div", "legend");
  const pin = (cls, label) => `<span class="pin ${cls}"><span>${label}</span></span>`;
  div.innerHTML = `<div>${pin("sample", 1)} номер в маршруте</div>
    <div>${pin("sample emergency", "")} авария</div>
    <div>${pin("unassigned", "!")} не назначена</div>
    <div>${pin("cancelled", "×")} отменена</div>
    <div>${pin("pending changed", "")} изменилась после события</div>`;
  return div;
};
legend.addTo(map);
map.on("click", (ev) => {
  if (!state.pickPoint) return;
  state.draft.location = { lat: +ev.latlng.lat.toFixed(6), lon: +ev.latlng.lng.toFixed(6) };
  state.pickPoint = false;
  $("map").classList.remove("picking");
  renderMap();
  renderCard();
});

// --- утилиты ---

const hhmm = (m) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
const parseHhmm = (s) => { const [h, m] = s.split(":").map(Number); return h * 60 + m; };
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const window_ = (r) => `${hhmm(r.window_start)}–${hhmm(r.window_end)}`;
const sign = (x, digits = 0) => (x > 0 ? "+" : x < 0 ? "−" : "±") + Math.abs(x).toFixed(digits);
const latLng = (l) => l && [l.lat, l.lon];
const initials = (name) => name.replace(/\./g, " ").split(/\s+/).filter(Boolean).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
// Цвет текста поверх цвета инженера: в палитре есть светлые цвета, на них белый не читается.
function inkOn(hex) {
  const n = parseInt(hex.slice(1), 16);
  return 0.299 * (n >> 16) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255) > 170 ? "#111318" : "#fff";
}
const colorStyle = (engId) => `background:${color(engId)};color:${inkOn(color(engId))}`;
const avatar = (e, size = "") => `<span class="avatar ${size}" style="${colorStyle(e.id)}">${esc(initials(e.name))}</span>`;
const ICON = {
  office: `<svg class="i" viewBox="0 0 24 24"><path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/></svg>`,
  close: `<svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>`,
  map: `<svg class="i" viewBox="0 0 24 24"><path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2zM9 4v14m6-12v14"/></svg>`,
};

async function api(path, body) {
  const res = await fetch(path, body instanceof FormData ? { method: "POST", body } : body ? {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  } : undefined);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(typeof err.detail === "string" ? err.detail : `${res.status} ${res.statusText}`);
  }
  return res.json();
}

function setStatus(text, error = false) {
  $("status").textContent = text;
  $("status").title = text;
  $("status").classList.toggle("error", error);
}

const plan = () => state.plans[state.algo];
const parentPlan = (p = plan()) => (p?.parent_id ? state.byId[p.parent_id] : null);
const diff = (p = plan()) => (p ? state.diffs[p.id] : null);
const allRequests = () => [...state.data.requests, ...(plan()?.extra_requests ?? [])];
const requestById = (id) => allRequests().find((r) => r.id === id);
const engineerById = (id) => state.data.engineers.find((e) => e.id === id);
const color = (engId) => COLORS[state.data.engineers.findIndex((e) => e.id === engId) % COLORS.length];
const isCancelled = (id) => plan()?.cancelled.includes(id);
const isOff = (engId) => plan()?.unavailable?.includes(engId);

function assignmentOf(requestId, p = plan()) {
  for (const route of p?.routes ?? []) {
    const i = route.stops.findIndex((s) => s.request_id === requestId);
    if (i >= 0) return { route, stop: route.stops[i], index: i };
  }
  return null;
}

// Состояние зафиксированной остановки на момент последнего события: выполнена / в работе / инженер в пути.
function stopState(route, index) {
  const now = plan()?.now;
  if (now == null || index >= route.frozen) return null;
  const s = route.stops[index];
  return s.end <= now ? "done" : s.start <= now ? "work" : "road";
}
const STOP_STATE = { done: "✓ выполнена", work: "в работе", road: "в пути" };
const stateTag = (status) => (status ? `<span class="tag ${status}">${STOP_STATE[status]}</span>` : "");

// Заявки, изменившиеся относительно плана до события (подсветка на карте и в списках).
function changedIds() {
  const d = diff();
  if (!d || !state.showDiff) return new Set();
  return new Set([...d.moved.map((m) => m.request_id), ...d.added, ...d.newly_assigned, ...d.newly_unassigned]);
}

function describeEvent(ev) {
  if (!ev) return "";
  const at = hhmm(ev.time);
  if (ev.type === "cancel") return `${at} — отмена заявки ${ev.request_id}`;
  if (ev.type === "unavailable") {
    const name = engineerById(ev.engineer_id)?.name ?? ev.engineer_id;
    return `${at} — ${name} недоступен (текущую работу ${ev.finish_current ? "доделывает" : "возвращает в пул"})`;
  }
  const where = ev.address || (ev.location ? "точка на карте" : "");
  return `${at} — срочная заявка «${ev.type_bk}»${where ? `, ${where}` : ""}`;
}

// --- загрузка ---

async function init() {
  const areas = await fillAreas();
  $("area").onchange = () => loadArea($("area").value);
  $("plan-btn").onclick = runPlans;
  $("event-btn").onclick = () => openEvent({});
  $("upload-btn").onclick = () => { closePanel(); state.panel = "upload"; state.selected = null; render(); };
  for (const btn of $("algo-toggle").querySelectorAll("button")) {
    btn.onclick = () => { state.algo = btn.dataset.algo; closePanel(); render(); };
  }
  await loadArea(areas[0].id);
}

async function fillAreas(selected) {
  const areas = await api("/api/areas");
  $("area").innerHTML = areas.map((a) => `<option value="${a.id}" ${a.id === selected ? "selected" : ""}>
    ${a.uploaded ? "⬆ " : ""}${esc(a.name)} — ${a.requests} заявок, ${a.engineers} инж.</option>`).join("");
  return areas;
}

async function loadArea(area) {
  Object.assign(state, { area, plans: {}, selected: null });
  $("area").value = area;
  closePanel();
  state.data = await api(`/api/areas/${area}/data`);
  setStatus("Нажмите «Спланировать»");
  render();
  fitMap();
}

function remember(p) {
  state.byId[p.id] = p;
  return p;
}

async function runPlans() {
  const area = state.area;
  const timeLimit = Number($("time-limit").value) || 30;
  setBusy(true);
  try {
    setStatus("Базовый алгоритм…");
    const baseline = remember(await api("/api/plan", { area, algorithm: "baseline" }));
    setStatus(`Оптимизация (до ${timeLimit} с)…`);
    const optimized = remember(await api("/api/plan", { area, algorithm: "optimized", time_limit: timeLimit }));
    if (area !== state.area) return; // пока считали, переключили участок
    Object.assign(state, { plans: { baseline, optimized }, algo: "optimized", selected: null });
    closePanel();
    setStatus(`Готово за ${elapsed()} с`);
    render();
  } catch (e) {
    setStatus(`Ошибка: ${e.message}`, true);
  } finally {
    setBusy(false);
  }
}

// Таймер операции (планирование, перепланирование, загрузка): тикает, пока идёт, затем остаётся итог.
const timer = { start: 0, id: null };
const elapsed = () => ((performance.now() - timer.start) / 1000).toFixed(1);

function setBusy(busy) {
  state.busy = busy;
  clearInterval(timer.id);
  if (busy) {
    timer.start = performance.now();
    timer.id = setInterval(() => { $("timer").textContent = `⏱ ${elapsed()} с`; }, 100);
  }
  $("timer").textContent = `⏱ ${busy ? "0.0" : elapsed()} с`;
  $("timer").classList.toggle("running", busy);
  $("status").classList.toggle("busy", busy);
  $("plan-btn").disabled = busy;
  $("event-btn").disabled = busy || !plan();
  $("upload-btn").disabled = busy;
  for (const id of ["event-submit", "upload-submit"]) if ($(id)) $(id).disabled = busy;
}

// --- событие и перепланирование ---

function openEvent(preset) {
  const p = plan();
  if (!p) return;
  state.draft = {
    type: "urgent", time: Math.max(p.now ?? 0, 13 * 60), type_bk: "Глобальная проблема", address: "",
    location: null, duration: "", request_id: "", engineer_id: "", finish_current: true, ...preset,
  };
  state.panel = "event";
  state.selected = null;
  render();
}

function closePanel() {
  Object.assign(state, { panel: null, draft: null, pickPoint: false });
  $("map").classList.remove("picking");
}

function eventPayload(d) {
  const base = { type: d.type, time: d.time };
  if (d.type === "cancel") return { ...base, request_id: d.request_id };
  if (d.type === "unavailable") return { ...base, engineer_id: d.engineer_id, finish_current: d.finish_current };
  return {
    ...base, type_bk: d.type_bk, address: d.address.trim() || null, location: d.location,
    duration: d.duration ? Number(d.duration) : null,
  };
}

async function submitEvent() {
  const d = state.draft;
  const missing = d.type === "cancel" && !d.request_id ? "Выберите заявку"
    : d.type === "unavailable" && !d.engineer_id ? "Выберите инженера"
    : d.type === "urgent" && !d.address.trim() && !d.location ? "Укажите адрес или точку на карте" : null;
  if (missing) { setStatus(missing, true); return; }

  const old = plan();
  const timeLimit = Math.min(Number($("time-limit").value) || 30, 20);
  setBusy(true);
  setStatus(`Перепланирование (до ${timeLimit} с + расстояния до новой точки)…`);
  try {
    const res = await api("/api/replan", { plan_id: old.id, event: eventPayload(d), time_limit: timeLimit });
    if (plan()?.id !== old.id) return; // пока считали, сменили участок или план
    const p = remember(res.plan);
    state.diffs[p.id] = res.diff;
    state.plans[state.algo] = p;
    closePanel();
    state.showDiff = true;
    // Новую срочную заявку сразу открываем: видно, кому она ушла и почему.
    const added = res.diff.added[0];
    state.selected = added ? { type: "request", id: added } : null;
    setStatus(`Перепланировано за ${elapsed()} с`);
    render();
    const pts = [...changedIds()].map((id) => latLng(requestById(id)?.location)).filter(Boolean);
    if (pts.length) map.fitBounds(pts, { padding: [60, 60], maxZoom: 13 });
  } catch (e) {
    setStatus(`Ошибка: ${e.message}`, true);
  } finally {
    setBusy(false);
  }
}

function undoEvent() {
  const parent = parentPlan();
  if (!parent) return;
  state.plans[state.algo] = parent;
  state.selected = null;
  setStatus("Событие откатено — показан предыдущий план");
  render();
}

// --- отрисовка ---

function render() {
  for (const btn of $("algo-toggle").querySelectorAll("button")) {
    btn.disabled = !state.plans[btn.dataset.algo];
    btn.classList.toggle("active", btn.dataset.algo === state.algo && !btn.disabled);
  }
  $("event-btn").disabled = state.busy || !plan();
  const p = plan();
  $("now").innerHTML = p?.now != null ? `<span class="now">Сейчас ${hhmm(p.now)}</span>` : "";
  $("summary").textContent = p?.summary ?? "";
  renderMap();
  renderEngineers();
  renderUnassigned();
  renderCard();
  renderMetrics();
}

function pinIcon(req, cls, label, bg) {
  const emergency = req.skill === EMERGENCY ? " emergency" : "";
  return L.divIcon({
    className: "", iconSize: [22, 22], iconAnchor: [11, 11],
    html: `<div class="pin ${cls}${emergency}" style="${bg ? `background:${bg};color:${inkOn(bg)}` : ""}"><span>${label}</span></div>`,
  });
}

// Линия маршрута по дорогам, пока геометрия не пришла (или без сети) — прямыми от точки к точке.
function routeLine(p, route, officeLatLng) {
  const road = state.geometry[p.id]?.[route.engineer_id];
  if (road) return road;
  return [officeLatLng, ...route.stops.map((s) => latLng(requestById(s.request_id)?.location))].filter(Boolean);
}

function loadGeometry(p) {
  if (!p || state.geometry[p.id] !== undefined) return;
  state.geometry[p.id] = null; // запрос в полёте
  api(`/api/plan/${p.id}/geometry`)
    .then((res) => {
      state.geometry[p.id] = res.routes;
      if (plan()?.id === p.id || parentPlan()?.id === p.id) renderMap();
    })
    .catch(() => {}); // остаются прямые линии
}

function renderMap() {
  layer.clearLayers();
  const { office } = state.data;
  const officeLatLng = latLng(office.location);
  if (officeLatLng) {
    L.marker(officeLatLng, { icon: L.divIcon({ className: "", html: `<div class="office-pin">${ICON.office}</div>`,
      iconSize: [30, 30], iconAnchor: [15, 15] }), zIndexOffset: 1000 })
      .bindTooltip(`Офис: ${esc(office.address)}`).addTo(layer);
  }

  const p = plan();
  const sel = state.selected?.type === "engineer" ? state.selected.id : null;
  const d = state.showDiff ? diff() : null;
  const parent = d && parentPlan();
  loadGeometry(p);
  loadGeometry(parent);
  const changedEng = (id) => d.changed_engineers.includes(id);
  if (parent) { // маршруты изменившихся инженеров до события — пунктиром
    for (const route of parent.routes) {
      if (!route.stops.length || !changedEng(route.engineer_id) || (sel && sel !== route.engineer_id)) continue;
      L.polyline(routeLine(parent, route, officeLatLng), { color: color(route.engineer_id), weight: 2, opacity: 0.75, dashArray: "4 6" })
        .bindTooltip(`${esc(engineerById(route.engineer_id).name)}: маршрут до события`).addTo(layer);
    }
  }
  for (const route of p?.routes ?? []) {
    if (!route.stops.length) continue;
    const selected = sel === route.engineer_id;
    // В diff‑режиме неизменившиеся маршруты приглушены, чтобы изменения читались.
    const opacity = sel ? (selected ? 0.85 : 0.12) : parent && !changedEng(route.engineer_id) ? 0.3 : 0.85;
    L.polyline(routeLine(p, route, officeLatLng), { color: color(route.engineer_id), weight: selected ? 5 : 3, opacity })
      .on("click", () => select("engineer", route.engineer_id)).addTo(layer);
  }

  const changed = changedIds();
  for (const req of allRequests()) {
    if (!req.location) continue;
    const a = p && assignmentOf(req.id);
    const hl = changed.has(req.id) ? " changed" : "";
    let icon;
    if (!p) icon = pinIcon(req, "pending", "");
    else if (isCancelled(req.id)) icon = pinIcon(req, "cancelled", "×");
    else if (a) {
      const done = stopState(a.route, a.index) === "done" ? " done" : "";
      icon = pinIcon(req, hl + done, a.index + 1, color(a.route.engineer_id));
    } else icon = pinIcon(req, "unassigned" + hl, "!");
    const tip = `${esc(req.id)} · ${window_(req)}` + (a ? ` · ${esc(engineerById(a.route.engineer_id).name)}` : "")
      + (isCancelled(req.id) ? " · отменена" : "");
    L.marker(latLng(req.location), { icon, zIndexOffset: hl ? 500 : 0 })
      .bindTooltip(tip).on("click", () => select("request", req.id)).addTo(layer);
  }

  const draft = state.panel === "event" && state.draft.type === "urgent" ? state.draft.location : null;
  if (draft) {
    L.marker(latLng(draft), { icon: L.divIcon({ className: "", iconSize: [22, 22], iconAnchor: [11, 11],
      html: `<div class="pin draft emergency"><span>?</span></div>` }), zIndexOffset: 1000 })
      .bindTooltip("Новая срочная заявка").addTo(layer);
  }
}

function fitMap() {
  const pts = [state.data.office, ...state.data.requests].map((x) => latLng(x.location)).filter(Boolean);
  if (pts.length) map.fitBounds(pts, { padding: [20, 20] });
}

function renderEngineers() {
  const { engineers, catalogs } = state.data;
  const p = plan();
  const d = state.showDiff ? diff() : null;
  const routeOf = (id) => p?.routes.find((r) => r.engineer_id === id);
  $("eng-count").textContent = p ? `${p.metrics.engineers_used} / ${engineers.length}` : engineers.length;
  $("eng-count").title = p ? "задействовано / всего" : "";
  $("engineers").innerHTML = engineers.map((e) => {
    const r = routeOf(e.id);
    const load = r ? (r.stops.length ? `${r.stops.length} з. · ${r.km.toFixed(1)} км` : "без заявок") : "";
    const skills = e.skills.map((s) => catalogs.skills[s].split(" ")[0]).join(", ");
    const cls = [
      r && !r.stops.length ? "idle" : "",
      isOff(e.id) ? "off" : "",
      d?.changed_engineers.includes(e.id) ? "changed" : "",
      isPicked("engineer", e.id) ? "selected" : "",
    ].join(" ");
    return `<li data-id="${e.id}" class="${cls}">${avatar(e)}<div>
      <div class="row-top"><b>${esc(e.name)}</b>${isOff(e.id) ? `<span class="tag bad">недоступен</span>` : ""}
        <span class="aside">${load}</span></div>
      <div class="sub">${hhmm(e.shift_start)}–${hhmm(e.shift_end)} · ${esc(catalogs.transports[e.transport])} · ${esc(skills)}</div>
      ${timeline(e, r)}</div></li>`;
  }).join("");
  for (const li of $("engineers").children) li.onclick = () => select("engineer", li.dataset.id);
}

// Мини‑таймлайн: дорожка — от самого раннего начала смены до самого позднего конца по участку,
// на ней смена инженера, работы по заявкам (цветом инженера) и отметка «сейчас» после события.
function timeline(e, r) {
  const es = state.data.engineers;
  const from = Math.min(...es.map((x) => x.shift_start)), to = Math.max(...es.map((x) => x.shift_end));
  const pct = (m) => ((Math.min(Math.max(m, from), to) - from) / (to - from)) * 100;
  const seg = (a, b, cls, style = "") => `<i class="${cls}" style="left:${pct(a)}%;width:${pct(b) - pct(a)}%;${style}"></i>`;
  const now = plan()?.now;
  return `<div class="tl" title="Смена ${hhmm(e.shift_start)}–${hhmm(e.shift_end)}">${seg(e.shift_start, e.shift_end, "shift")}
    ${(r?.stops ?? []).map((s, i) => seg(s.start, s.end, `job ${stopState(r, i) === "done" ? "done" : ""}`, `background:${color(e.id)}`)).join("")}
    ${now != null ? `<i class="now-mark" style="left:${pct(now)}%"></i>` : ""}</div>`;
}

function renderUnassigned() {
  const list = plan()?.unassigned ?? [];
  const changed = changedIds();
  $("unassigned-count").textContent = plan() ? list.length : "";
  $("unassigned").innerHTML = !plan() ? "" : !list.length
    ? `<li class="sub" style="cursor:default">Все заявки распределены</li>`
    : list.map((u) => {
      const r = requestById(u.request_id);
      const sel = isPicked("request", u.request_id) ? " selected" : "";
      const em = r.skill === EMERGENCY;
      return `<li data-id="${u.request_id}" class="unassigned-item${sel}${changed.has(u.request_id) ? " changed" : ""}">
        <span class="mark${em ? " emergency" : ""}" title="${em ? "Авария" : ""}">!</span><div>
        <div class="row-top"><b>${esc(u.request_id)}</b><span class="aside">${window_(r)}</span></div>
        <div class="sub">${esc(r.type_hd ?? r.type_bk)}</div>
        <div class="reason">${esc(u.reason)}</div></div></li>`;
    }).join("");
  for (const li of $("unassigned").querySelectorAll("li[data-id]")) li.onclick = () => select("request", li.dataset.id);
}

// Объект выбран: открыта его карточка или он подставлен в форму события.
function isPicked(type, id) {
  if (state.panel === "event") return state.draft[type === "request" ? "request_id" : "engineer_id"] === id;
  return state.selected?.type === type && state.selected.id === id;
}

// Пока открыта форма события, клик по подходящему объекту (заявка при отмене, инженер при недоступности)
// подставляет его в форму; любой другой клик закрывает панель и открывает карточку объекта.
function select(type, id) {
  const d = state.panel === "event" ? state.draft : null;
  if (d && (type === "request" ? d.type === "cancel" : d.type === "unavailable")) {
    d[type === "request" ? "request_id" : "engineer_id"] = id;
    render();
    return;
  }
  if (state.panel) closePanel();
  const same = state.selected?.type === type && state.selected.id === id;
  state.selected = same ? null : { type, id };
  render();
  if (!state.selected) return;
  if (type === "request") {
    const loc = requestById(id)?.location;
    if (loc) map.panTo(latLng(loc));
  } else {
    const r = plan()?.routes.find((x) => x.engineer_id === id);
    const pts = (r?.stops ?? []).map((s) => latLng(requestById(s.request_id)?.location)).filter(Boolean);
    if (pts.length) map.fitBounds(pts, { padding: [40, 40], maxZoom: 14 });
  }
}

// Правая панель: форма события, карточка выбранного объекта или обзор плана.
function renderCard() {
  const s = state.selected;
  if (state.panel === "event") $("card").innerHTML = eventForm();
  else if (state.panel === "upload") $("card").innerHTML = uploadForm();
  else if (s) $("card").innerHTML = s.type === "engineer" ? engineerCard(s.id) : requestCard(s.id);
  else $("card").innerHTML = overview();
  bindCard();
}

// Обработчики элементов панели навешиваются после каждой отрисовки.
function bindCard() {
  const card = $("card");
  for (const a of card.querySelectorAll("[data-req]")) {
    a.onclick = (ev) => { ev.preventDefault(); select("request", a.dataset.req); };
  }
  for (const a of card.querySelectorAll("[data-eng]")) {
    a.onclick = (ev) => { ev.preventDefault(); select("engineer", a.dataset.eng); };
  }
  const on = (id, fn) => { const el = $(id); if (el) el.onclick = fn; };
  on("card-close", () => { state.selected = null; render(); });
  on("undo-btn", undoEvent);
  on("diff-toggle", () => { state.showDiff = !state.showDiff; render(); });
  on("cancel-req-btn", () => openEvent({ type: "cancel", request_id: state.selected.id }));
  on("off-eng-btn", () => openEvent({ type: "unavailable", engineer_id: state.selected.id }));
  on("event-close", () => { closePanel(); render(); });
  on("event-submit", submitEvent);
  on("upload-submit", submitUpload);
  on("upload-close", () => { closePanel(); render(); });
  on("pick-btn", () => {
    state.pickPoint = !state.pickPoint;
    $("map").classList.toggle("picking", state.pickPoint);
    renderCard();
  });

  const form = $("event-form");
  if (!form) return;
  for (const el of form.querySelectorAll("[name]")) {
    el.oninput = el.onchange = () => {
      const d = state.draft;
      const v = el.type === "checkbox" ? el.checked : el.value;
      if (el.name === "time") { if (v) d.time = parseHhmm(v); return; }
      d[el.name] = v;
      if (el.name === "type") {
        state.pickPoint = false;
        $("map").classList.remove("picking");
      }
      // смена типа или выбор из списка меняет подсветку в списках и на карте; поля ввода не перерисовываем
      if (["type", "engineer_id", "request_id"].includes(el.name)) render();
    };
  }
}

const closeBtn = `<button id="card-close" class="btn quiet icon" title="Закрыть" aria-label="Закрыть">${ICON.close}</button>`;

function engineerCard(id) {
  const e = engineerById(id);
  const { catalogs } = state.data;
  const p = plan();
  const r = p?.routes.find((x) => x.engineer_id === id);
  let html = `<div class="card-head">${avatar(e, "lg")}<div><h3>${esc(e.name)}</h3>
    <div class="sub">${esc(id)} · смена ${hhmm(e.shift_start)}–${hhmm(e.shift_end)}
      ${isOff(id) ? `<span class="tag bad">недоступен</span>` : ""}</div></div>${closeBtn}</div>`;
  if (r) {
    const load = p.metrics.shift_load[id];
    html += `<div class="stats"><div><b>${r.stops.length}</b><span>заявок</span></div>
      <div><b>${r.km.toFixed(1)}</b><span>км</span></div>
      <div><b>${r.travel_min}</b><span>мин в пути</span></div>
      ${load !== undefined ? `<div><b>${Math.round(load * 100)}%</b><span>загрузка</span></div>` : ""}</div>`;
  }
  // Строка маршрута с сервера повторяет цифры выше — показываем её, только если заявок нет.
  html += `${r?.explanation && !r.stops.length ? `<p class="explain">${esc(r.explanation)}</p>` : ""}
    <dl><dt>Транспорт</dt><dd>${esc(catalogs.transports[e.transport])}</dd>
    <dt>Навыки</dt><dd>${e.skills.map((k) => esc(catalogs.skills[k])).join("<br>")}</dd></dl>`;
  const changed = changedIds();
  if (r?.stops.length) {
    html += `<h4>Расписание · ${hhmm(r.departure)}–${hhmm(r.stops.at(-1).end)}</h4><ol class="stops">
      <li class="stop"><span class="stop-n office">${ICON.office}</span><div>
        <div class="row-top"><b>Офис</b><span class="aside">выезд ${hhmm(r.departure)}</span></div></div></li>` +
      r.stops.map((st, i) => {
        const q = requestById(st.request_id);
        const status = stopState(r, i);
        const cls = `${status === "done" ? "done" : ""} ${changed.has(st.request_id) ? "changed" : ""}`;
        return `<li class="stop ${cls}"><span class="stop-n" style="${colorStyle(id)}">${i + 1}</span><div>
          <div class="row-top"><a href="#" data-req="${st.request_id}"><b>${esc(st.request_id)}</b></a>${stateTag(status)}
            <span class="aside">${hhmm(st.start)}–${hhmm(st.end)}</span></div>
          <div class="sub">${esc(q.address)}</div>
          <div class="sub">окно ${window_(q)} · приезд ${hhmm(st.arrival)}${st.wait ? ` · ждёт ${st.wait} мин` : ""} · +${st.leg_km.toFixed(1)} км</div>
        </div></li>`;
      }).join("") + "</ol>";
  }
  if (p && !isOff(id)) html += `<div class="actions"><button id="off-eng-btn" class="btn sm danger">Инженер недоступен…</button></div>`;
  return html;
}

function requestCard(id) {
  const r = requestById(id);
  const { catalogs } = state.data;
  const p = plan();
  const a = p && assignmentOf(id);
  const u = p?.unassigned.find((x) => x.request_id === id);
  const geo = { exact: "дом", street: "улица", approx: "приблизительно (район/город)" }[r.geo_quality] ?? "—";
  const status = a ? stopState(a.route, a.index) : null;
  let html = `<div class="card-head"><div><h3>Заявка ${esc(id)}</h3>
    <div class="sub">${esc(r.type_bk)}${r.type_hd ? ` / ${esc(r.type_hd)}` : ""}${r.gigabit ? " · Гбит" : ""}
      ${r.skill === EMERGENCY ? `<span class="tag emergency">авария</span>` : ""}
      ${isCancelled(id) ? `<span class="tag">отменена</span>` : ""}${stateTag(status)}</div></div>${closeBtn}</div>`;
  if (a) {
    const eng = engineerById(a.route.engineer_id);
    html += `<div class="stats"><div><b>${hhmm(a.stop.start)}</b><span>начало работы</span></div>
      <div><b>${window_(r)}</b><span>окно</span></div>
      <div><b>№${a.index + 1}</b><span>в маршруте</span></div></div>`;
    html += explanationBlock(p.id, id);
    html += `<dl><dt>Инженер</dt><dd><span class="swatch" style="background:${color(eng.id)}"></span><a href="#" data-eng="${eng.id}">${esc(eng.name)}</a></dd>
      <dt>Приезд</dt><dd>${hhmm(a.stop.arrival)}${a.stop.wait ? ` (ждёт ${a.stop.wait} мин)` : ""}</dd>
      <dt>Работа</dt><dd>${hhmm(a.stop.start)}–${hhmm(a.stop.end)}</dd></dl><h4>Заявка</h4>`;
  }
  if (u) html += `<div class="callout bad"><b>Не назначена:</b> ${esc(u.reason)}</div>`;
  const was = state.showDiff && diff() ? assignmentOf(id, parentPlan()) : null;
  if (was && a && was.route.engineer_id !== a.route.engineer_id) {
    html += `<div class="callout changed">До события: ${esc(engineerById(was.route.engineer_id).name)}, начало ${hhmm(was.stop.start)}</div>`;
  }
  html += `<dl>
    <dt>Адрес</dt><dd>${esc(r.address)}</dd>
    ${a ? "" : `<dt>Окно</dt><dd>${window_(r)}</dd>`}
    <dt>Длительность</dt><dd>${r.duration} мин</dd>
    <dt>Навык</dt><dd>${esc(catalogs.skills[r.skill])}</dd>
    <dt>Приоритет</dt><dd>${esc(catalogs.priorities[r.priority])}</dd>
    <dt>Транспорт</dt><dd>${r.required_transport ? esc(catalogs.transports[r.required_transport]) : "любой"}</dd>
    <dt>Геокод</dt><dd>${geo}</dd></dl>`;
  const started = a && p.now != null && a.stop.start <= p.now;
  if (p && !isCancelled(id) && !started) html += `<div class="actions"><button id="cancel-req-btn" class="btn sm danger">Отменить заявку…</button></div>`;
  return html;
}

// Объяснение назначения грузится с сервера один раз на пару (план, заявка).
function explanationBlock(planId, requestId) {
  const key = `${planId}/${requestId}`;
  const lines = state.explain[key];
  if (lines) return `<div class="explain">${lines.map((l) => `<div>${esc(l)}</div>`).join("")}</div>`;
  if (lines === undefined) {
    state.explain[key] = null; // запрос в полёте
    api(`/api/plan/${planId}/explain/${encodeURIComponent(requestId)}`)
      .then((res) => { state.explain[key] = res.lines; })
      .catch((e) => { state.explain[key] = [`Объяснение недоступно: ${e.message}`]; })
      .finally(() => {
        if (!state.panel && state.selected?.id === requestId && plan()?.id === planId) renderCard();
      });
  }
  return `<div class="explain hint">Объяснение…</div>`;
}

function eventForm() {
  const d = state.draft;
  const p = plan();
  const { catalogs, engineers } = state.data;
  const radio = (value, label) =>
    `<label><input type="radio" name="type" value="${value}" ${d.type === value ? "checked" : ""}> ${label}</label>`;
  let fields;
  if (d.type === "urgent") {
    fields = `<label>Тип заявки <select name="type_bk">${catalogs.types_bk.map((t) =>
        `<option ${t === d.type_bk ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></label>
      <label>Адрес <input name="address" value="${esc(d.address)}" placeholder="Москва, ул. …, д. …"></label>
      <div class="row"><button id="pick-btn" class="btn sm ${state.pickPoint ? "active" : ""}">${ICON.map}${state.pickPoint ? "Кликните по карте…" : "Точка на карте"}</button>
        <span class="sub">${d.location ? `${d.location.lat.toFixed(5)}, ${d.location.lon.toFixed(5)}` : "без точки адрес геокодируется"}</span></div>
      <label>Работа, мин <input name="duration" type="number" min="1" max="600" value="${esc(d.duration)}" placeholder="по справочнику"></label>
      <p class="sub">Окно — от момента события до конца дня, приоритет «срочная»: пропуск штрафуется как у аварии.</p>`;
  } else if (d.type === "cancel") {
    const options = allRequests().filter((r) => !isCancelled(r.id)).map((r) => {
      const a = assignmentOf(r.id);
      const who = a ? `${engineerById(a.route.engineer_id).name}, ${hhmm(a.stop.start)}` : "не назначена";
      return `<option value="${r.id}" ${r.id === d.request_id ? "selected" : ""}>${esc(r.id)} · ${window_(r)} · ${esc(who)}</option>`;
    });
    fields = `<label>Заявка <select name="request_id"><option value="">— выберите или кликните на карте —</option>${options.join("")}</select></label>
      <p class="sub">Отменить можно только ещё не начатую заявку.</p>`;
  } else {
    const options = engineers.filter((e) => !isOff(e.id)).map((e) =>
      `<option value="${e.id}" ${e.id === d.engineer_id ? "selected" : ""}>${esc(e.name)} · ${hhmm(e.shift_start)}–${hhmm(e.shift_end)}</option>`);
    fields = `<label>Инженер <select name="engineer_id"><option value="">— выберите —</option>${options.join("")}</select></label>
      <label class="check"><input type="checkbox" class="accent-check" name="finish_current" ${d.finish_current ? "checked" : ""}> текущую работу доделывает</label>
      <p class="sub">Его будущие заявки уйдут другим инженерам, выполненные останутся за ним.</p>`;
  }
  return `<div class="card-head"><div><h3>Новое событие</h3><div class="sub">перепланирование показанного плана</div></div></div>
    <div id="event-form" class="form">
      <div class="segmented">${radio("urgent", "Срочная заявка")}${radio("cancel", "Отмена")}${radio("unavailable", "Инженер выбыл")}</div>
      <label>Время события <input name="time" type="time" value="${hhmm(d.time)}"></label>
      ${fields}
      <p class="sub">Выполненное, текущая работа и заявки, к которым инженер уже выехал, фиксируются. Остальное перепланирует
        ${p.algorithm === "optimized" ? "оптимизатор с минимумом перестановок" : "базовый алгоритм"}.</p>
      <div class="row"><button id="event-submit" class="btn primary" ${state.busy ? "disabled" : ""}>Перепланировать</button>
        <button id="event-close" class="btn">Закрыть</button></div>
    </div>`;
}

// Панель без выбранного объекта: что изменилось после события + сравнение с базовым вариантом.
function overview() {
  const p = plan();
  if (!p) return `<div class="empty">${ICON.map}<b>План ещё не построен</b>
    Нажмите «Спланировать» — будут построены базовый и оптимизированный планы для сравнения.</div>`;
  const d = diff();
  return (d ? diffBlock(p, d) : "") + compareTable()
    + `<p class="hint mt">Выберите инженера или заявку — на карте или в списке, — чтобы увидеть расписание и объяснение.</p>`;
}

function diffBlock(p, d) {
  const name = (id) => esc(engineerById(id)?.name ?? id);
  const req = (id) => `<a href="#" data-req="${id}">${esc(id)}</a>`;
  const items = [];
  for (const id of d.added) {
    const a = assignmentOf(id);
    items.push(`Новая ${req(id)} → ${a ? `${name(a.route.engineer_id)}, начало ${hhmm(a.stop.start)}` : "<span class=\"worse\">не назначена</span>"}`);
  }
  if (d.cancelled.length) items.push(`Отменена ${d.cancelled.map(req).join(", ")}`);
  for (const m of d.moved) items.push(`${req(m.request_id)}: ${name(m.from)} → ${name(m.to)}`);
  const reassigned = d.newly_assigned.filter((id) => !d.added.includes(id));
  if (reassigned.length) items.push(`<span class="better">Снова назначены: ${reassigned.map(req).join(", ")}</span>`);
  if (d.newly_unassigned.length) items.push(`<span class="worse">Сняты: ${d.newly_unassigned.map(req).join(", ")}</span>`);
  if (d.retimed.length) items.push(`Сдвинуто время у ${d.retimed.length} заявок (инженер тот же)`);
  if (!items.length) items.push("Назначения не изменились");

  const delta = (k, label, digits, lowerIsBetter) => {
    const x = d.metrics[k].delta;
    const cls = x === 0 ? "" : (x < 0) === lowerIsBetter ? "better" : "worse";
    return `<span class="${cls}">${label} ${sign(x, digits)}</span>`;
  };
  return `<div class="diff">
    <h3>Событие ${esc(describeEvent(p.event))}</h3>
    <ul class="plain">${items.map((x) => `<li>${x}</li>`).join("")}</ul>
    <p class="deltas">${delta("assigned", "выполнено", 0, false)}${delta("engineers_used", "инженеров", 0, true)}${delta("total_km", "км", 1, true)}</p>
    <p class="sub">Изменились маршруты: ${d.changed_engineers.map(name).join(", ") || "—"}</p>
    <div class="row actions"><label class="check"><input type="checkbox" class="accent-check" id="diff-toggle" ${state.showDiff ? "checked" : ""}>
      изменения на карте</label><button id="undo-btn" class="btn sm">Откатить событие</button></div>
  </div>`;
}

// --- загрузка своих данных ---

function uploadForm() {
  const options = [...$("area").options].map((o) => `<option value="${o.value}">${esc(o.textContent.trim())}</option>`).join("");
  return `<div class="card-head"><div><h3>Загрузить данные</h3><div class="sub">свои заявки и/или инженеры</div></div></div>
    <div id="upload-form" class="form">
      <label>Название <input name="name" placeholder="Мой участок"></label>
      <label>Заявки, CSV <input name="requests" type="file" accept=".csv,text/csv"></label>
      <p class="sub">Формат как в датасете: cp1251 или UTF‑8, разделитель «;», колонки «Заявка», «Тип заявки BK»,
        «Начало», «Окончание» (ДД.ММ.ГГГГ ЧЧ:ММ), «Адрес»; в конце строка «Адрес офиса;…».</p>
      <label>Инженеры, JSON <input name="engineers" type="file" accept=".json,application/json"></label>
      <p class="sub">Массив как в data/engineers_*.json: id, name, shift_start/shift_end (минуты или "09:00"),
        skills (1 — локальные, 2 — подключения, 3 — аварийные), transport (1 авто, 2 пешком, 3 велосипед, 4 ОТ).
        Без файла инженеры берутся из участка‑основы или генерируются по шаблону.</p>
      <label>Участок‑основа <select name="base_area"><option value="">— нет —</option>${options}</select></label>
      <label>Адрес офиса <input name="office_address" placeholder="если нет в CSV"></label>
      <p class="sub">Новые адреса геокодируются (≈1 с на адрес), расстояния считаются по дорогам — это может занять минуту‑две.</p>
      <div class="row"><button id="upload-submit" class="btn primary" ${state.busy ? "disabled" : ""}>Загрузить</button>
        <button id="upload-close" class="btn">Закрыть</button></div>
    </div>`;
}

async function submitUpload() {
  const form = new FormData();
  for (const el of $("upload-form").querySelectorAll("[name]")) {
    if (el.type === "file") { if (el.files[0]) form.append(el.name, el.files[0]); }
    else if (el.value.trim()) form.append(el.name, el.value.trim());
  }
  if (!form.has("requests") && !form.has("base_area")) {
    setStatus("Выберите CSV с заявками или участок‑основу", true);
    return;
  }
  setBusy(true);
  setStatus("Загрузка: геокодирование и расстояния…");
  try {
    const res = await api("/api/upload", form);
    await fillAreas(res.id);
    await loadArea(res.id);
    setStatus(`Загружено за ${elapsed()} с: ${res.requests} заявок, ${res.engineers} инженеров${res.warnings.length ? ". " + res.warnings.join(". ") : ""}`,
      res.warnings.length > 0);
  } catch (e) {
    setStatus(`Ошибка загрузки: ${e.message}`, true);
  } finally {
    setBusy(false);
  }
}

// «Базовый vs оптимизированный»: обязательные метрики ТЗ — инженеры и км по каждому и суммарно.
function compareTable() {
  const { baseline: b, optimized: o } = state.plans;
  if (!b || !o) return "";
  const routeOf = (p, id) => p.routes.find((r) => r.engineer_id === id);
  const cell = (r) => (r?.stops.length ? `${r.km.toFixed(1)} <span class="sub">(${r.stops.length})</span>` : "—");
  const cmp = (x, y, lowerIsBetter, fmt = (v) => v, fmtY = fmt) => {
    const cls = x === y ? "" : (y < x) === lowerIsBetter ? "better" : "worse";
    return `<td>${fmt(x)}</td><td class="${cls}">${fmtY(y)}</td>`;
  };
  const note = b.now != null || o.now != null ? `<p class="sub">Планы сравниваются в текущем состоянии, с учётом событий.</p>` : "";
  const rows = state.data.engineers.map((e) => {
    const rb = routeOf(b, e.id), ro = routeOf(o, e.id);
    if (!rb?.stops.length && !ro?.stops.length) return "";
    return `<tr><td><span class="swatch" style="background:${color(e.id)}"></span><a href="#" data-eng="${e.id}">${esc(e.name)}</a></td><td>${cell(rb)}</td><td>${cell(ro)}</td></tr>`;
  }).join("");
  const mb = b.metrics, mo = o.metrics;
  return `<h4 style="margin-top:0">Базовый vs оптимизированный</h4>${note}
    <table class="compare">
      <tr><th></th><th>Базовый</th><th>Оптим.</th></tr>
      <tr><td>Выполнено</td>${cmp(mb.assigned, mo.assigned, false, (v) => `${v} из ${mb.total}`, (v) => `${v} из ${mo.total}`)}</tr>
      <tr><td>Инженеров</td>${cmp(mb.engineers_used, mo.engineers_used, true)}</tr>
      <tr><td>Пробег, км</td>${cmp(mb.total_km, mo.total_km, true, (v) => v.toFixed(1))}</tr>
      <tr><td>км на заявку</td>${cmp(mb.km_per_request, mo.km_per_request, true, (v) => v.toFixed(2))}</tr>
      <tr><td>Аварии</td>${cmp(mb.by_skill.emergency.assigned, mo.by_skill.emergency.assigned, false)}</tr>
      <tr><td>Подключения</td>${cmp(mb.by_skill.connection.assigned, mo.by_skill.connection.assigned, false)}</tr>
      <tr class="section"><th colspan="3">км по инженерам (заявок)</th></tr>
      ${rows}
    </table>`;
}

const kpi = (label, value, extra = "", cls = "") =>
  `<div class="kpi ${cls}"><span class="kpi-label">${label}</span><span class="kpi-value">${value}</span>${extra}</div>`;

function renderMetrics() {
  const p = plan();
  if (!p) { // до планирования — сводка по участку
    const { requests, engineers } = state.data;
    const count = (skill) => requests.filter((r) => r.skill === skill).length;
    $("metrics").innerHTML = kpi("Заявок", requests.length) + kpi("Инженеров", engineers.length)
      + kpi("Аварий", count(EMERGENCY)) + kpi("Подключений", count(2)) + kpi("Локальных", count(1))
      + `<div class="kpi hint">Метрики плана появятся после планирования</div>`;
    return;
  }
  const m = p.metrics;
  // После события сравниваем с планом до него, иначе оптимизированный — с базовым.
  const parent = parentPlan();
  const [other, label] = parent ? [parent.metrics, "до события"]
    : state.algo === "optimized" && state.plans.baseline ? [state.plans.baseline.metrics, "базовый"] : [null, ""];
  const cmp = (key, fmt, lowerIsBetter, digits = 0) => {
    if (!other) return "";
    const d = m[key] - other[key];
    const cls = Math.abs(d) < 1e-9 ? "" : (d < 0) === lowerIsBetter ? "better" : "worse";
    return `<span class="kpi-delta"><b class="${cls}">${Math.abs(d) < 1e-9 ? "=" : sign(d, digits)}</b> · ${label} ${fmt(other[key])}</span>`;
  };
  const skill = (k, name) => {
    const { assigned, total } = m.by_skill[k];
    return `<div class="skill-row"><span>${name}</span><span class="bar"><i style="width:${total ? (assigned / total) * 100 : 0}%"></i></span>
      <span>${assigned}/${total}</span></div>`;
  };
  const fixed1 = (x) => x.toFixed(1), fixed2 = (x) => x.toFixed(2), id = (x) => x;
  $("metrics").innerHTML = [
    kpi("План", p.algorithm === "optimized" ? "Оптимизированный" : "Базовый",
      p.now != null ? `<span class="kpi-delta">после события ${hhmm(p.now)}</span>` : "", "algo"),
    kpi("Выполнено", `${m.assigned} <small>из ${m.total}</small>`, cmp("assigned", id, false)),
    kpi("Инженеров", m.engineers_used, cmp("engineers_used", id, true)),
    kpi("Пробег, км", fixed1(m.total_km), cmp("total_km", fixed1, true, 1)),
    kpi("км на заявку", fixed2(m.km_per_request), cmp("km_per_request", fixed2, true, 2)),
    `<div class="kpi skills">${skill("emergency", "Аварии")}${skill("connection", "Подключения")}${skill("local", "Локальные")}</div>`,
    p.violations.length
      ? `<div class="kpi bad" title="${esc(p.violations.join("\n"))}"><span class="kpi-label">Нарушения</span><span class="kpi-value">✗ ${p.violations.length}</span></div>`
      : kpi("Нарушения", "✓ 0", `<span class="kpi-delta">все ограничения соблюдены</span>`, "ok"),
  ].join("");
}

init().catch((e) => setStatus(`Ошибка: ${e.message}`, true));
