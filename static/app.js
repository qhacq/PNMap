"use strict";

// Palette catégorielle validée pour le daltonisme, doublée d'une forme par poule.
const POOL_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"];
const POOL_SHAPES = ["circle", "square", "triangle", "diamond", "circle", "square"];
const VERSION_NAMES = { current: "Poules actuelles", optimal: "Version optimale", mine: "Ma version" };
const HISTORY_MAX = 200;

const S = {
  teams: [], pools: [], byId: {}, idx: {},
  R: null, H: null, D: null,        // matrices aller-retour km, aller-retour heures, aller simple km
  matrixSource: null, matrixReady: false,
  V: { current: null, optimal: null, mine: null },   // id -> poule
  optimalInfo: null, optimalFresh: false,
  view: "mine",
  history: [], hpos: 0,             // entrées {label, before, after, change}
  lastChange: null,
  locks: new Set(),
  pairs: [],
  settings: {},
  linkMode: "none",
  selected: null,
  geoSelected: null, geoPicking: false,
  jobRunning: false,
};

// ------------------------------------------------------------------ utils
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const fmt = (x, d = 0) => (x == null || Number.isNaN(x)) ? "-" :
  Number(x).toLocaleString("fr-FR", { minimumFractionDigits: d, maximumFractionDigits: d });
const signed = (x, d = 0) => (x > 0.0001 ? "+" : "") + fmt(x, d);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const poolIndex = (p) => Math.max(0, S.pools.indexOf(p));
const poolColor = (p) => POOL_COLORS[poolIndex(p) % POOL_COLORS.length];
const shortName = (n) => n.replace(/\s+-\s+\d+$/, "").slice(0, 30);

function shapeSvg(p, size = 18, ring = "#fff") {
  const k = poolIndex(p), c = POOL_COLORS[k % POOL_COLORS.length];
  const shape = POOL_SHAPES[k % POOL_SHAPES.length];
  const body = {
    circle: `<circle cx="9" cy="9" r="7"/>`,
    square: `<rect x="2.5" y="2.5" width="13" height="13" rx="2"/>`,
    triangle: `<polygon points="9,1.5 17,16 1,16"/>`,
    diamond: `<polygon points="9,1 17,9 9,17 1,9"/>`,
  }[shape];
  return `<svg width="${size}" height="${size}" viewBox="0 0 18 18" aria-hidden="true"><g fill="${c}" stroke="${ring}" stroke-width="2">${body}</g></svg>`;
}

/** Variation colorée : en vert si favorable (moins de km), en rouge sinon. */
function delta(v, ref, unit = "km", d = 0) {
  if (v == null || ref == null) return "-";
  const dv = v - ref;
  const cls = dv > 0.05 ? "pos" : dv < -0.05 ? "neg" : "";
  const pct = ref ? ` (${signed(100 * dv / ref, 1)} %)` : "";
  return `<span class="delta ${cls}">${signed(dv, d)} ${unit}${unit === "km" ? pct : ""}</span>`;
}

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.body = JSON.stringify(opts.body);
    init.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, init);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) { /* corps non JSON */ }
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return opts.raw ? res : res.json();
}

function busy(text) {
  $("#busy").classList.toggle("hidden", !text);
  $("#busy-text").textContent = text || "";
}

const toIntKeys = (o) => o ? Object.fromEntries(Object.entries(o).map(([k, v]) => [Number(k), v])) : null;

let saveTimer = null;
function saveUi() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    api("/api/ui-state", {
      method: "PUT",
      body: {
        mine: S.V.mine, history: S.history.slice(-HISTORY_MAX), hpos: Math.min(S.hpos, HISTORY_MAX),
        lastChange: S.lastChange, locks: [...S.locks], pairs: S.pairs, settings: readSettings(),
        view: S.view, linkMode: S.linkMode, labels: $("#show-labels").checked,
      },
    }).catch(() => {});
  }, 400);
}

// ------------------------------------------------------------------ calculs (côté navigateur, instantanés)
async function loadMatrix() {
  const m = await api("/api/matrix");
  const n = m.ids.length;
  S.idx = Object.fromEntries(m.ids.map((id, i) => [id, i]));
  S.D = m.km;
  S.R = Array.from({ length: n }, (_, i) => Array.from({ length: n }, (_, j) => m.km[i][j] + m.km[j][i]));
  S.H = Array.from({ length: n }, (_, i) => Array.from({ length: n }, (_, j) => (m.minutes[i][j] + m.minutes[j][i]) / 60));
  S.Mmin = m.minutes;
  S.matrixSource = m.source;
}

/** Somme des allers-retours de chaque équipe vers chaque poule : sums[i][p]. */
function poolSums(A) {
  const sums = S.teams.map(() => Object.fromEntries(S.pools.map(p => [p, 0])));
  for (const a of S.teams) {
    const i = S.idx[a.id];
    for (const b of S.teams) {
      if (a.id !== b.id && A[b.id] in sums[i]) sums[i][A[b.id]] += S.R[i][S.idx[b.id]];
    }
  }
  return sums;
}

/** Indicateurs complets d'une répartition. */
function metricsOf(A) {
  if (!S.R || !A) return null;
  const teams = {};
  for (const t of S.teams) {
    const i = S.idx[t.id];
    let km = 0, hours = 0, longest = null;
    for (const u of S.teams) {
      if (u.id === t.id || A[u.id] !== A[t.id]) continue;
      const j = S.idx[u.id];
      km += S.R[i][j];
      hours += S.H[i][j];
      if (!longest || S.D[i][j] > longest.km) longest = { to: u.id, km: S.D[i][j] };
    }
    teams[t.id] = { km, hours, longest, pool: A[t.id], moved: A[t.id] !== t.current_pool };
  }
  const pools = {};
  for (const p of S.pools) {
    const ids = S.teams.filter(t => A[t.id] === p).map(t => t.id);
    const km = ids.reduce((s, id) => s + teams[id].km, 0);
    const worst = ids.reduce((w, id) => (!w || teams[id].km > teams[w].km) ? id : w, null);
    const lg = ids.reduce((w, id) => (teams[id].longest && (!w || teams[id].longest.km > w.km))
      ? { from: id, to: teams[id].longest.to, km: teams[id].longest.km } : w, null);
    pools[p] = { size: ids.length, km, hours: ids.reduce((s, id) => s + teams[id].hours, 0),
      avg: ids.length ? km / ids.length : 0, worst, longest: lg };
  }
  const all = Object.keys(teams).map(Number);
  const worst = all.reduce((w, id) => (!w || teams[id].km > teams[w].km) ? id : w, null);
  const longest = Object.values(pools).reduce((w, p) => p.longest && (!w || p.longest.km > w.km) ? p.longest : w, null);
  return {
    teams, pools, worst, longest,
    total: all.reduce((s, id) => s + teams[id].km, 0),
    hours: all.reduce((s, id) => s + teams[id].hours, 0),
    moved: all.filter(id => teams[id].moved).length,
  };
}

/** Impact de l'échange a <-> b (poules différentes) sur la répartition A. */
function swapImpact(A, a, b, sums = poolSums(A)) {
  const i = S.idx[a], j = S.idx[b], P = A[a], Q = A[b], r = S.R[i][j];
  const aBefore = sums[i][P], aAfter = sums[i][Q] - r;
  const bBefore = sums[j][Q], bAfter = sums[j][P] - r;
  const dP = 2 * ((sums[j][P] - r) - sums[i][P]);
  const dQ = 2 * ((sums[i][Q] - r) - sums[j][Q]);
  return { a, b, from: P, to: Q, aBefore, aAfter, bBefore, bAfter, dP, dQ, dTotal: dP + dQ };
}

function bestSwaps(A, limit = 10) {
  const sums = poolSums(A);
  const out = [];
  for (const x of S.teams) {
    for (const y of S.teams) {
      if (x.id >= y.id || A[x.id] === A[y.id] || S.locks.has(x.id) || S.locks.has(y.id)) continue;
      const s = swapImpact(A, x.id, y.id, sums);
      if (s.dTotal < -0.05) out.push(s);
    }
  }
  return out.sort((u, v) => u.dTotal - v.dTotal).slice(0, limit);
}

// ------------------------------------------------------------------ Ma version : modifications et historique
function commit(label, after, change = null) {
  const before = { ...S.V.mine };
  S.history = S.history.slice(0, S.hpos);
  S.history.push({ label, before, after: { ...after }, change });
  if (S.history.length > HISTORY_MAX) S.history.shift();
  S.hpos = S.history.length;
  S.V.mine = { ...after };
  S.lastChange = change;
  if (S.view !== "mine") setView("mine");
  renderAll();
  saveUi();
}

function applySwap(a, b) {
  const A = S.V.mine;
  const imp = swapImpact(A, a, b);
  const after = { ...A, [a]: A[b], [b]: A[a] };
  commit(`Échange ${shortName(S.byId[a].name)} (${A[a]}) ↔ ${shortName(S.byId[b].name)} (${A[b]})`, after, imp);
}

function initMine(source) {
  const src = S.V[source];
  if (!src) return;
  commit(`Ma version réinitialisée depuis ${VERSION_NAMES[source].toLowerCase()}`, src, null);
}

function undo() {
  if (S.hpos <= 0) return;
  const e = S.history[--S.hpos];
  S.V.mine = { ...e.before };
  S.lastChange = S.hpos > 0 ? S.history[S.hpos - 1].change : null;
  renderAll();
  saveUi();
}

function redo() {
  if (S.hpos >= S.history.length) return;
  const e = S.history[S.hpos++];
  S.V.mine = { ...e.after };
  S.lastChange = e.change;
  renderAll();
  saveUi();
}

// ------------------------------------------------------------------ carte
const map = L.map("map").setView([48.7, 5.9], 8);
const geoMap = L.map("geo-map").setView([48.7, 5.9], 8);
for (const m of [map, geoMap]) {
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18, attribution: '&copy; contributeurs <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(m);
}
const markers = {};
const linkLayer = L.layerGroup().addTo(map);
const routeLayer = L.layerGroup().addTo(map);
const geoLayer = L.layerGroup().addTo(geoMap);
let fitted = false;

function teamLatLng(t) {
  // Décalage léger pour les équipes partageant une commune.
  const twins = S.teams.filter(u => u.located && u.lat === t.lat && u.lon === t.lon);
  const k = twins.indexOf(t);
  return [t.lat + 0.012 * k, t.lon + 0.018 * k];
}

function teamInfoHtml(t, A, M) {
  const r = M?.teams[t.id];
  const lg = r?.longest;
  return `<b>${esc(t.name)}</b><br>${esc(t.commune || "commune inconnue")} · poule <b>${esc(A[t.id])}</b>` +
    (r ? `<br>${fmt(r.km)} km par saison (${fmt(r.hours, 1)} h)` : "") +
    (lg ? `<br>Trajet le plus long : ${fmt(lg.km)} km vers ${esc(shortName(S.byId[lg.to].name))}` : "") +
    (A[t.id] !== t.current_pool ? `<br><i>Poule actuelle : ${esc(t.current_pool)}</i>` : "");
}

function renderMap() {
  const A = S.V[S.view];
  if (!A) return;
  const M = metricsOf(A);
  const showLabels = $("#show-labels").checked;
  const bounds = [];
  for (const t of S.teams) {
    let mk = markers[t.id];
    if (!t.located) {
      if (mk) { mk.remove(); delete markers[t.id]; }
      continue;
    }
    const ll = teamLatLng(t);
    const sel = S.selected === t.id;
    const icon = L.divIcon({
      className: "pin" + (sel ? " sel" : ""), html: shapeSvg(A[t.id], sel ? 24 : 18, sel ? "#111" : "#fff"),
      iconSize: sel ? [24, 24] : [18, 18], iconAnchor: sel ? [12, 12] : [9, 9],
    });
    if (!mk) {
      mk = L.marker(ll, { icon, riseOnHover: true }).addTo(map);
      mk.on("click", () => selectTeam(t.id));
      mk.on("mouseover", () => mk.openPopup());
      mk.on("mouseout", () => { if (S.selected !== t.id) mk.closePopup(); });
      markers[t.id] = mk;
    } else {
      mk.setIcon(icon);
      mk.setLatLng(ll);
    }
    mk.setZIndexOffset(sel ? 1000 : 0);
    mk.bindPopup(teamInfoHtml(t, A, M), { autoPan: false, closeButton: false, offset: [0, -6] });
    mk.unbindTooltip();
    if (showLabels) {
      mk.bindTooltip(esc(shortName(t.name)), { permanent: true, direction: "right", offset: [9, 0], className: "lbl" });
    }
    bounds.push(ll);
  }
  if (!fitted && bounds.length) { map.fitBounds(bounds, { padding: [20, 20] }); fitted = true; }
  renderLinks(A);
  renderRoutes(A);
  $("#legend").innerHTML = S.pools.map(p => `<span>${shapeSvg(p, 14)} Poule ${esc(p)}${M ? ` · ${fmt(M.pools[p].km)} km` : ""}</span>`).join("")
    + `<span class="muted">${esc(VERSION_NAMES[S.view])}</span>`;
}

function convexHull(points) {
  const pts = [...points].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  if (pts.length < 3) return pts;
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lower = [], upper = [];
  for (const p of pts) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop();
    lower.push(p);
  }
  for (const p of pts.reverse()) {
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop();
    upper.push(p);
  }
  return lower.slice(0, -1).concat(upper.slice(0, -1));
}

function renderLinks(A) {
  linkLayer.clearLayers();
  if (S.linkMode === "none") return;
  for (const p of S.pools) {
    const pts = S.teams.filter(t => t.located && A[t.id] === p).map(teamLatLng);
    if (!pts.length) continue;
    const color = poolColor(p);
    if (S.linkMode === "hull") {
      const hull = convexHull(pts.map(([la, lo]) => [lo, la])).map(([lo, la]) => [la, lo]);
      L.polygon(hull, { color, weight: 2, dashArray: "6 4", fillColor: color, fillOpacity: 0.08, interactive: false }).addTo(linkLayer);
    } else {
      const c = [pts.reduce((s, q) => s + q[0], 0) / pts.length, pts.reduce((s, q) => s + q[1], 0) / pts.length];
      for (const q of pts) L.polyline([c, q], { color, weight: 1.5, opacity: 0.7, interactive: false }).addTo(linkLayer);
      L.marker(c, { interactive: false, icon: L.divIcon({ className: "bary", html: `<span style="border-color:${color}">${esc(p)}</span>`, iconSize: [22, 22], iconAnchor: [11, 11] }) }).addTo(linkLayer);
    }
  }
}

function renderRoutes(A) {
  routeLayer.clearLayers();
  const t = S.byId[S.selected];
  if (!t || !t.located) return;
  for (const u of S.teams) {
    if (u.id === t.id || !u.located || A[u.id] !== A[t.id]) continue;
    L.polyline([teamLatLng(t), teamLatLng(u)], { color: "#374151", weight: 1.5, opacity: 0.7, dashArray: "3 4", interactive: false })
      .addTo(routeLayer);
  }
}

function selectTeam(id) {
  const prev = S.selected;
  S.selected = prev === id ? null : id;
  if (prev && markers[prev]) markers[prev].closePopup();
  renderMap();
  renderPools();
  if (S.selected && markers[S.selected]) markers[S.selected].openPopup();
  const el = S.selected && $(`.team[data-id="${S.selected}"]`);
  if (el) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function setView(v) {
  if (!S.V[v]) return;
  S.view = v;
  $$("#view-seg button").forEach(b => b.setAttribute("aria-checked", String(b.dataset.view === v)));
  saveUi();
}

// ------------------------------------------------------------------ versions côte à côte
function renderVersions() {
  const box = $("#versions");
  const mOpt = metricsOf(S.V.optimal), mCur = metricsOf(S.V.current);
  box.innerHTML = ["current", "optimal", "mine"].map(v => {
    const A = S.V[v];
    const M = metricsOf(A);
    let status = "";
    if (v === "current") status = `<span class="tag">clubs.csv</span>`;
    if (v === "optimal") {
      const o = S.optimalInfo;
      status = !o ? `<span class="tag warn">${S.jobRunning ? "calcul en cours…" : "non calculée"}</span>`
        : `<span class="tag ${o.proven ? "ok" : "warn"}">${o.proven ? "✔ optimale prouvée" : `meilleure trouvée (écart à la borne ${fmt(100 * (o.gap || 0), 1)} %)`}</span>` +
          (!S.optimalFresh ? ` <span class="tag warn">matrice modifiée : à recalculer</span>` : "") +
          (Object.keys(o.params?.locks || {}).length ? ` <span class="tag">${Object.keys(o.params.locks).length} verrou(s)</span>` : "") +
          `<div class="muted small">calculée le ${esc(o.computed_at || "")} en ${fmt(o.wall_time, 1)} s</div>`;
    }
    if (v === "mine") status = `<span class="tag">${S.hpos} modification(s)</span>`;
    const comp = !M ? "" : [
      v !== "optimal" && mOpt ? `<div>vs optimale : ${delta(M.total, mOpt.total)}</div>` : "",
      v !== "current" && mCur ? `<div>vs actuelles : ${delta(M.total, mCur.total)}</div>` : "",
    ].join("");
    const pools = M ? `<div class="vpools">${S.pools.map(p => `<span>${shapeSvg(p, 12)} ${esc(p)} ${fmt(M.pools[p].km)}</span>`).join("")}</div>` : "";
    const actions = v === "mine"
      ? `<div class="buttons"><span class="small muted">Initialiser depuis :</span>
          <button data-init="current">Poules actuelles</button>
          <button data-init="optimal" ${S.V.optimal ? "" : "disabled"}>Version optimale</button></div>`
      : (v === "optimal" ? `<div class="buttons"><button data-goto-opt>Recalculer (Optimiser)</button></div>` : "");
    return `<div class="version ${S.view === v ? "active" : ""}" data-version="${v}">
      <div class="vhead"><h3>${VERSION_NAMES[v]}</h3>
        <button class="small-btn" data-show="${v}" ${A ? "" : "disabled"}>${S.view === v ? "affichée" : "Afficher"}</button></div>
      <div class="vtotal">${M ? `${fmt(M.total)} <small>km cumulés</small>` : `<small class="muted">${S.matrixReady ? "-" : "matrice en attente"}</small>`}</div>
      ${status}${comp}${pools}${actions}</div>`;
  }).join("");
  $$("[data-show]", box).forEach(b => b.addEventListener("click", () => { setView(b.dataset.show); renderAll(); }));
  $$("[data-init]", box).forEach(b => b.addEventListener("click", () => initMine(b.dataset.init)));
  $$("[data-goto-opt]", box).forEach(b => b.addEventListener("click", () => {
    $("#btn-optimize").scrollIntoView({ behavior: "smooth", block: "center" });
    $("#btn-optimize").focus();
  }));
}

// ------------------------------------------------------------------ impact de Ma version
function renderImpact() {
  const box = $("#impact");
  const M = metricsOf(S.V.mine), O = metricsOf(S.V.optimal), C = metricsOf(S.V.current);
  if (!M) { box.innerHTML = `<p class="muted">Disponible quand la matrice des distances est calculée.</p>`; return; }
  const name = id => esc(shortName(S.byId[id].name));
  let html = `<div class="kpis">
    <div class="kpi wide"><div class="v">${fmt(M.total)} km</div><div class="l">km cumulés · ${fmt(M.hours)} h</div></div>
    <div class="kpi"><div class="v">${O ? delta(M.total, O.total) : "-"}</div><div class="l">vs version optimale</div></div>
    <div class="kpi"><div class="v">${C ? delta(M.total, C.total) : "-"}</div><div class="l">vs poules actuelles</div></div>
  </div>`;
  const ch = S.lastChange;
  if (ch) {
    html += `<div class="last-change"><b>Dernier échange</b> (total ${delta(ch.dTotal, 0).replace(/ \(.*\)/, "")})
      <table class="mini">
        <tr><th></th><th>avant</th><th>après</th><th></th></tr>
        <tr><td>${shapeSvg(ch.to, 11)} ${name(ch.a)} <small>${esc(ch.from)}→${esc(ch.to)}</small></td><td>${fmt(ch.aBefore)}</td><td>${fmt(ch.aAfter)}</td><td>${delta(ch.aAfter, ch.aBefore).replace(/ \(.*\)/, "")}</td></tr>
        <tr><td>${shapeSvg(ch.from, 11)} ${name(ch.b)} <small>${esc(ch.to)}→${esc(ch.from)}</small></td><td>${fmt(ch.bBefore)}</td><td>${fmt(ch.bAfter)}</td><td>${delta(ch.bAfter, ch.bBefore).replace(/ \(.*\)/, "")}</td></tr>
        <tr><td>Poule ${esc(ch.from)}</td><td colspan="2"></td><td>${delta(ch.dP, 0).replace(/ \(.*\)/, "")}</td></tr>
        <tr><td>Poule ${esc(ch.to)}</td><td colspan="2"></td><td>${delta(ch.dQ, 0).replace(/ \(.*\)/, "")}</td></tr>
      </table></div>`;
  }
  html += `<table class="mini pools-mini"><tr><th>Poule</th><th>km</th><th>vs optimale</th><th>vs actuelles</th></tr>` +
    S.pools.map(p => `<tr><td>${shapeSvg(p, 11)} ${esc(p)}</td><td>${fmt(M.pools[p].km)}</td>
      <td>${O ? delta(M.pools[p].km, O.pools[p].km).replace(/ \(.*\)/, "") : "-"}</td>
      <td>${C ? delta(M.pools[p].km, C.pools[p].km).replace(/ \(.*\)/, "") : "-"}</td></tr>`).join("") + `</table>`;
  if (M.worst) html += `<p class="small">Équipe la plus pénalisée : <b>${name(M.worst)}</b> (poule ${esc(S.V.mine[M.worst])}, ${fmt(M.teams[M.worst].km)} km)</p>`;
  if (M.longest) html += `<p class="small">Trajet le plus long : <b>${name(M.longest.from)}</b> → <b>${name(M.longest.to)}</b> (${fmt(M.longest.km)} km)</p>`;
  box.innerHTML = html;
}

function renderHistory() {
  $("#btn-undo").disabled = S.hpos <= 0;
  $("#btn-redo").disabled = S.hpos >= S.history.length;
  const items = S.history.map((e, k) => {
    const d = e.change ? ` <span class="delta ${e.change.dTotal > 0 ? "pos" : "neg"}">${signed(e.change.dTotal)} km</span>` : "";
    return `<li class="${k >= S.hpos ? "undone" : ""}">${esc(e.label)}${d}</li>`;
  });
  $("#history").innerHTML = items.reverse().join("") || `<li class="muted">Aucune modification.</li>`;
}

function renderBestSwaps() {
  const box = $("#best-swaps");
  if (!S.R || !S.V.mine) { box.innerHTML = `<p class="muted">Matrice en attente.</p>`; return; }
  const list = bestSwaps(S.V.mine);
  const M = metricsOf(S.V.mine);
  if (!list.length) {
    box.innerHTML = `<p class="muted">Aucun échange de deux équipes ne réduit le kilométrage total : Ma version est un optimum local${S.locks.size ? " (équipes verrouillées exclues)" : ""}.</p>`;
    return;
  }
  const name = id => `${id}. ${esc(shortName(S.byId[id].name))}`;
  box.innerHTML = `<table class="swaps"><thead><tr><th>#</th><th>Équipe</th><th></th><th>Équipe</th>
    <th>Gain total</th><th>Équipe 1 avant → après</th><th>Équipe 2 avant → après</th><th></th></tr></thead><tbody>` +
    list.map((s, k) => `<tr>
      <td>${k + 1}</td>
      <td>${shapeSvg(s.from, 11)} ${name(s.a)}</td><td>${esc(s.from)} ⇄ ${esc(s.to)}</td>
      <td>${shapeSvg(s.to, 11)} ${name(s.b)}</td>
      <td>${delta(M.total + s.dTotal, M.total)}</td>
      <td>${fmt(s.aBefore)} → ${fmt(s.aAfter)}</td><td>${fmt(s.bBefore)} → ${fmt(s.bAfter)}</td>
      <td><button data-swap="${s.a},${s.b}">Appliquer</button></td></tr>`).join("") + `</tbody></table>`;
  $$("[data-swap]", box).forEach(b => b.addEventListener("click", () => {
    const [a, c] = b.dataset.swap.split(",").map(Number);
    applySwap(a, c);
  }));
}

// ------------------------------------------------------------------ colonnes de poules
function renderPools() {
  const box = $("#pools");
  const A = S.V[S.view];
  const editable = S.view === "mine";
  $("#pools-title").textContent = `Poules : ${VERSION_NAMES[S.view]}`;
  $("#pools-hint").innerHTML = editable
    ? "Glisser une équipe vers une autre poule (ou bouton ⇄) : choisir l'équipe à échanger, impact affiché avant validation. 🔒 verrouille l'équipe pour l'optimisation."
    : `Lecture seule. <button id="btn-copy-view">Copier dans Ma version</button>`;
  const copy = $("#btn-copy-view");
  if (copy) copy.addEventListener("click", () => initMine(S.view));
  box.style.setProperty("--n", S.pools.length);
  box.innerHTML = "";
  if (!A) return;
  const M = metricsOf(A), O = metricsOf(S.V.optimal);
  const sizes = readSettings().pool_sizes;
  for (const p of S.pools) {
    const pm = M?.pools[p];
    const members = S.teams.filter(t => A[t.id] === p)
      .sort((a, b) => (M?.teams[b.id].km ?? 0) - (M?.teams[a.id].km ?? 0) || a.id - b.id);
    const col = document.createElement("div");
    col.className = "pool";
    col.style.setProperty("--c", poolColor(p));
    col.dataset.pool = p;
    col.innerHTML = `
      <div class="pool-head">
        <h3><span>${shapeSvg(p, 16)} Poule ${esc(p)}</span><span class="count ${members.length === sizes[p] ? "" : "bad"}">${members.length} / ${sizes[p]}</span></h3>
        <div class="stats">${pm ? `<b>${fmt(pm.km)} km</b> cumulés · ${fmt(pm.avg)} km/équipe · ${fmt(pm.hours)} h` : "matrice en attente"}
        ${pm?.longest ? `<br>Plus long : ${esc(shortName(S.byId[pm.longest.from].name))} → ${esc(shortName(S.byId[pm.longest.to].name))} (${fmt(pm.longest.km)} km)` : ""}</div>
      </div>`;
    for (const t of members) col.appendChild(teamCard(t, A, M, O, editable));
    if (editable) {
      col.addEventListener("dragover", e => { e.preventDefault(); col.classList.add("over"); });
      col.addEventListener("dragleave", e => { if (!col.contains(e.relatedTarget)) col.classList.remove("over"); });
      col.addEventListener("drop", e => {
        e.preventDefault();
        col.classList.remove("over");
        const id = Number(e.dataTransfer.getData("text/plain"));
        const onCard = e.target.closest(".team");
        const partner = onCard ? Number(onCard.dataset.id) : null;
        if (id && S.V.mine[id] !== p) openSwap(id, p, partner);
      });
    }
    box.appendChild(col);
  }
}

function teamCard(t, A, M, O, editable) {
  const r = M?.teams[t.id];
  const el = document.createElement("div");
  const locked = S.locks.has(t.id);
  el.className = "team" + (M && M.worst === t.id ? " worst" : "") + (S.selected === t.id ? " selected" : "") + (locked && editable ? " locked" : "");
  el.dataset.id = t.id;
  el.draggable = editable && !locked;
  const dOpt = (O && r && S.view !== "optimal") ? r.km - O.teams[t.id].km : null;
  const others = S.pools.filter(p => p !== A[t.id]);
  el.innerHTML = `
    ${editable ? `<label class="lock" title="Verrouiller dans cette poule pour l'optimisation"><input type="checkbox" ${locked ? "checked" : ""}>🔒</label>` : `<span></span>`}
    <div class="tmain">
      <div class="name" title="${esc(t.name)}">${t.id}. ${esc(t.name)}</div>
      <div class="sub">${esc(t.commune || (t.located ? "point placé à la main" : "commune inconnue"))}${t.located ? "" : " · ⚠ non localisée"}
        ${A[t.id] !== t.current_pool ? `<span class="moved">· actuelle ${esc(t.current_pool)}</span>` : ""}</div>
    </div>
    <div class="km">${r ? `${fmt(r.km)} km` : "-"}
      ${r ? `<small>${fmt(r.hours, 1)} h${dOpt != null && Math.abs(dOpt) >= 0.5 ? ` · <span class="delta ${dOpt > 0 ? "pos" : "neg"}" title="écart avec la version optimale">${signed(dOpt)}</span>` : ""}</small>` : ""}
    </div>
    ${editable && !locked ? `<select class="move" title="Déplacer vers une autre poule (échange)"><option value="">⇄</option>${others.map(p => `<option value="${esc(p)}">→ ${esc(p)}</option>`).join("")}</select>` : `<span></span>`}`;
  if (editable) {
    el.querySelector(".lock input").addEventListener("change", e => {
      e.target.checked ? S.locks.add(t.id) : S.locks.delete(t.id);
      renderPools();
      renderBestSwaps();
      saveUi();
    });
    el.querySelector(".lock").addEventListener("click", e => e.stopPropagation());
    const sel = el.querySelector("select.move");
    if (sel) {
      sel.addEventListener("click", e => e.stopPropagation());
      sel.addEventListener("change", () => { if (sel.value) openSwap(t.id, sel.value); sel.value = ""; });
    }
  }
  el.addEventListener("click", () => selectTeam(t.id));
  el.addEventListener("dragstart", e => { e.dataTransfer.setData("text/plain", String(t.id)); el.classList.add("dragging"); });
  el.addEventListener("dragend", () => el.classList.remove("dragging"));
  return el;
}

// ------------------------------------------------------------------ fenêtre d'échange
let swapChoice = null;
function openSwap(id, dest, preferred = null) {
  const A = S.V.mine;
  if (!S.R) { setMessage("Matrice des distances indisponible.", "err"); return; }
  if (S.locks.has(id)) { setMessage("Équipe verrouillée : la déverrouiller pour la déplacer.", "err"); return; }
  const sums = poolSums(A);
  const M = metricsOf(A);
  const cands = S.teams.filter(u => A[u.id] === dest)
    .map(u => ({ u, s: swapImpact(A, id, u.id, sums), locked: S.locks.has(u.id) }))
    .sort((x, y) => (x.locked - y.locked) || x.s.dTotal - y.s.dTotal);
  const t = S.byId[id];
  $("#swap-title").innerHTML = `Déplacer ${shapeSvg(A[id], 14)} <b>${esc(t.name)}</b> de la poule ${esc(A[id])} vers ${shapeSvg(dest, 14)} la poule ${esc(dest)}`;
  const name = u => `${u.id}. ${esc(shortName(u.name))}`;
  $("#swap-table").innerHTML = `<thead><tr><th></th><th>Équipe envoyée en ${esc(A[id])}</th><th>Total</th>
      <th>${esc(shortName(t.name))}<br><small>avant → après</small></th><th>Partenaire<br><small>avant → après</small></th>
      <th>Poule ${esc(A[id])}</th><th>Poule ${esc(dest)}</th></tr></thead><tbody>` +
    cands.map(({ u, s, locked }) => `<tr data-partner="${u.id}" class="${locked ? "disabled" : ""}">
      <td><input type="radio" name="partner" value="${u.id}" ${locked ? "disabled" : ""}></td>
      <td>${name(u)}${locked ? " 🔒" : ""}</td>
      <td>${delta(M.total + s.dTotal, M.total)}</td>
      <td>${fmt(s.aBefore)} → ${fmt(s.aAfter)}</td>
      <td>${fmt(s.bBefore)} → ${fmt(s.bAfter)}</td>
      <td>${delta(s.dP, 0).replace(/ \(.*\)/, "")}</td><td>${delta(s.dQ, 0).replace(/ \(.*\)/, "")}</td></tr>`).join("") + `</tbody>`;
  swapChoice = null;
  $("#swap-ok").disabled = true;
  const pick = (pid) => {
    swapChoice = pid;
    $$("#swap-table tbody tr").forEach(tr => tr.classList.toggle("sel", Number(tr.dataset.partner) === pid));
    const radio = $(`#swap-table input[value="${pid}"]`);
    if (radio) radio.checked = true;
    $("#swap-ok").disabled = false;
  };
  $$("#swap-table tbody tr:not(.disabled)").forEach(tr => tr.addEventListener("click", () => pick(Number(tr.dataset.partner))));
  const first = cands.find(c => !c.locked && (preferred == null || c.u.id === preferred)) || cands.find(c => !c.locked);
  if (first) pick(first.u.id);
  const dlg = $("#swap-dialog");
  dlg.dataset.team = id;
  dlg.showModal();
}

$("#swap-dialog").addEventListener("close", () => {
  const dlg = $("#swap-dialog");
  if (dlg.returnValue === "ok" && swapChoice != null) applySwap(Number(dlg.dataset.team), swapChoice);
});

// ------------------------------------------------------------------ comparatif
function renderCompare() {
  const box = $("#compare");
  const vs = ["current", "optimal", "mine"].map(v => [v, metricsOf(S.V[v])]);
  if (vs.every(([, m]) => !m)) { box.innerHTML = `<p class="muted">Disponible quand la matrice est calculée.</p>`; return; }
  const O = vs[1][1], C = vs[0][1], Mi = vs[2][1];
  const name = id => id ? esc(shortName(S.byId[id].name)) : "";
  const row = (label, get, d = 0, unit = "km") => `<tr><td>${label}</td>${vs.map(([, m]) => `<td>${m ? fmt(get(m), d) : "-"}</td>`).join("")}
    <td>${Mi && O ? delta(get(Mi), get(O), unit, d) : "-"}</td><td>${Mi && C ? delta(get(Mi), get(C), unit, d) : "-"}</td></tr>`;
  box.innerHTML = `<table class="compare"><thead><tr><th></th>${vs.map(([v]) => `<th>${VERSION_NAMES[v]}</th>`).join("")}
    <th>Ma version vs optimale</th><th>Ma version vs actuelles</th></tr></thead><tbody>
    ${row("Km cumulés", m => m.total)}
    ${S.pools.map(p => row(`${shapeSvg(p, 11)} Poule ${esc(p)} (km)`, m => m.pools[p].km)).join("")}
    ${row("Heures cumulées", m => m.hours, 0, "h")}
    ${row("Km de l'équipe la plus pénalisée", m => m.teams[m.worst].km)}
    ${row("Trajet le plus long (km, aller)", m => m.longest?.km ?? 0)}
    ${row("Équipes changeant de poule", m => m.moved, 0, "")}
    <tr class="small"><td>Équipe la plus pénalisée</td>${vs.map(([, m]) => `<td>${m ? name(m.worst) : "-"}</td>`).join("")}<td colspan="2"></td></tr>
    <tr class="small"><td>Trajet le plus long</td>${vs.map(([, m]) => `<td>${m?.longest ? `${name(m.longest.from)} → ${name(m.longest.to)}` : "-"}</td>`).join("")}<td colspan="2"></td></tr>
    </tbody></table>`;
}

function renderAll() {
  renderVersions();
  renderMap();
  renderPools();
  renderImpact();
  renderHistory();
  renderBestSwaps();
  renderCompare();
  if ($("#tab-matrice").classList.contains("active")) renderMatrix();
}

// ------------------------------------------------------------------ réglages et optimisation
function readSettings() {
  const sizes = {};
  for (const p of S.pools) {
    const inp = $(`#size-${p}`);
    sizes[p] = inp ? Number(inp.value) : (S.settings.pool_sizes?.[p] ?? S.defaultSizes?.[p] ?? 12);
  }
  return {
    metric: $("#metric").value,
    time_limit: Number($("#time-limit").value) || 30,
    pool_sizes: sizes,
    separate_clubs: $("#separate-clubs").checked,
    stability_weight: Number($("#stability").value),
    fairness: Number($("#fairness").value),
  };
}

function applySettings(s) {
  if (s.metric) $("#metric").value = s.metric;
  if (s.time_limit) $("#time-limit").value = s.time_limit;
  if (s.separate_clubs !== undefined) $("#separate-clubs").checked = s.separate_clubs;
  if (s.stability_weight !== undefined) $("#stability").value = s.stability_weight;
  if (s.fairness !== undefined) $("#fairness").value = s.fairness;
  renderSliders();
}

function renderSliders() {
  const unit = $("#metric").value === "duration" ? "min" : "km";
  const st = Number($("#stability").value);
  $("#stability-val").textContent = st ? `${st} ${unit} par équipe déplacée` : "désactivée";
  const f = Number($("#fairness").value);
  $("#fairness-val").textContent = f === 0 ? "total uniquement" : f === 1 ? "pire cas uniquement" : `${Math.round(f * 100)} % pire cas`;
}

function renderPoolSizes(saved) {
  $("#pool-sizes").innerHTML = "Équipes par poule : " + S.pools.map(p => {
    const v = saved?.[p] ?? S.defaultSizes[p];
    return `<label style="flex-direction:row;align-items:center;gap:3px">${shapeSvg(p, 12)} <b>${esc(p)}</b>
      <input type="number" id="size-${p}" min="0" max="${S.teams.length}" value="${v}"></label>`;
  }).join("");
  $$("#pool-sizes input").forEach(i => i.addEventListener("change", () => { renderPools(); saveUi(); }));
}

function setMessage(text, cls = "") {
  const el = $("#opt-message");
  el.className = "message " + cls;
  el.textContent = text;
}

function applyOptimal(o) {
  S.optimalInfo = o;
  S.V.optimal = o ? toIntKeys(o.assignment) : null;
}

async function optimize() {
  const s = readSettings();
  const locks = {};
  for (const id of S.locks) locks[id] = S.V.mine[id];
  const body = {
    ...s, locks,
    forbidden_pairs: S.pairs.filter(p => p.type === "forbidden").map(p => [p.a, p.b]),
    imposed_pairs: S.pairs.filter(p => p.type === "imposed").map(p => [p.a, p.b]),
    hint: S.V.mine,
  };
  const t0 = Date.now();
  const tick = () => busy(`Optimisation CP-SAT en cours… ${Math.round((Date.now() - t0) / 1000)} s / ${s.time_limit} s`);
  tick();
  const timer = setInterval(tick, 250);
  try {
    const r = await api("/api/optimize", { method: "POST", body });
    if (!r.assignment) { setMessage(`${r.status} : ${r.message}`, "err"); return; }
    applyOptimal(r.optimal);
    S.optimalFresh = true;
    const gap = r.status !== "OPTIMAL" ? ` Écart à la borne : ${fmt(r.gap * 100, 1)} %.` : "";
    setMessage(`${r.message} ${fmt(r.optimal.total_km)} km cumulés en ${fmt(r.wall_time, 1)} s.${gap} ` +
      `Version optimale mise à jour${S.locks.size ? ` (${S.locks.size} équipe(s) verrouillée(s))` : ""}.`, "ok");
    setView("optimal");
    renderAll();
  } catch (e) {
    setMessage(e.message, "err");
  } finally {
    clearInterval(timer);
    busy(null);
  }
}

// ------------------------------------------------------------------ paires
function renderPairs() {
  const opts = S.teams.map(t => `<option value="${t.id}">${t.id}. ${esc(shortName(t.name))}</option>`).join("");
  for (const sel of ["#pair-a", "#pair-b"]) {
    const v = $(sel).value;
    $(sel).innerHTML = opts;
    if (v) $(sel).value = v;
  }
  $("#pairs").innerHTML = S.pairs.map((p, k) => `<li><span>${p.type === "imposed" ? "🔗" : "⛔"}
    ${esc(shortName(S.byId[p.a]?.name || p.a))} ${p.type === "imposed" ? "avec" : "séparée de"} ${esc(shortName(S.byId[p.b]?.name || p.b))}</span>
    <button data-k="${k}" title="Supprimer">✕</button></li>`).join("") || `<li class="muted">Aucune contrainte de paire.</li>`;
  $$("#pairs button").forEach(b => b.addEventListener("click", () => {
    S.pairs.splice(Number(b.dataset.k), 1);
    renderPairs();
    saveUi();
  }));
}

// ------------------------------------------------------------------ exports
async function doExport(btn) {
  const v = $("#export-version").value;
  const A = S.V[v];
  if (!A) { setMessage(`${VERSION_NAMES[v]} indisponible.`, "err"); return; }
  try {
    const res = await api(`/api/export/${btn.dataset.export}`, {
      method: "POST", raw: true,
      body: { assignment: A, label: VERSION_NAMES[v], kind: btn.dataset.kind || "distance" },
    });
    const blob = await res.blob();
    const fname = (res.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/)?.[1] || btn.dataset.export;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = fname;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  } catch (e) {
    setMessage(`Export impossible : ${e.message}`, "err");
  }
}

// ------------------------------------------------------------------ géocodage
const geoMarkers = {};
let geoFitted = false;

function renderGeo() {
  const all = $("#show-all-geo").checked;
  const review = S.teams.filter(t => t.needs_review);
  const badge = $("#review-badge");
  badge.textContent = review.length;
  badge.classList.toggle("hidden", !review.length);
  const rows = (all ? S.teams : review).map(t => {
    const state = !t.located ? `<span class="ko">non localisée</span>`
      : t.verified ? `<span class="ok">validée (${esc(t.geo_source)})</span>`
      : t.a_verifier ? `<span class="warn">à vérifier</span>` : `<span class="ok">${esc(t.geo_source)}</span>`;
    return `<tr data-id="${t.id}" class="${S.geoSelected === t.id ? "sel" : ""}">
      <td><b>${t.id}. ${esc(t.name)}</b><br><small>poule actuelle ${esc(t.current_pool)}</small></td>
      <td>${esc(t.commune) || "<i class='muted'>vide</i>"}</td>
      <td>${state}<div class="label">${esc(t.geo_label)}</div></td>
      <td><form data-id="${t.id}">
        <input type="text" placeholder="Adresse de la salle" name="address">
        <button type="submit">Chercher</button>
        <button type="button" data-act="pick">Placer sur la carte</button>
        ${t.located && !t.verified ? `<button type="button" data-act="validate">Valider</button>` : ""}
        ${t.geo_source && t.geo_source !== "nominatim" || t.verified ? `<button type="button" data-act="reset" title="Revenir au géocodage de la commune">Réinitialiser</button>` : ""}
      </form></td></tr>`;
  });
  $("#geo-rows").innerHTML = rows.join("") || `<tr><td colspan="4" class="ok">Toutes les équipes sont localisées et vérifiées.</td></tr>`;
  $$("#geo-rows tr[data-id]").forEach(tr => tr.addEventListener("click", e => {
    if (e.target.closest("input,button")) return;
    geoSelect(Number(tr.dataset.id), false);
  }));
  $$("#geo-rows form").forEach(f => {
    const id = Number(f.dataset.id);
    f.addEventListener("submit", e => {
      e.preventDefault();
      const address = f.address.value.trim();
      if (address) geoUpdate(id, { address });
    });
    f.querySelectorAll("button[data-act]").forEach(b => b.addEventListener("click", () => {
      if (b.dataset.act === "pick") geoSelect(id, true);
      if (b.dataset.act === "validate") geoAction(`/api/teams/${id}/validate`, "POST");
      if (b.dataset.act === "reset") geoAction(`/api/teams/${id}/location`, "DELETE");
    }));
  });
  renderGeoMap();
}

function renderGeoMap() {
  geoLayer.clearLayers();
  for (const k in geoMarkers) delete geoMarkers[k];
  const all = $("#show-all-geo").checked;
  for (const t of S.teams) {
    if (!t.located || (!all && !t.needs_review && t.id !== S.geoSelected)) continue;
    const sel = t.id === S.geoSelected;
    const mk = L.marker([t.lat, t.lon], { draggable: sel, opacity: sel ? 1 : 0.6 }).addTo(geoLayer);
    mk.bindTooltip(`${t.id}. ${esc(shortName(t.name))}`, { permanent: sel, direction: "top", offset: [-15, -12] });
    mk.on("click", e => {
      if (S.geoPicking) placePicked(e.latlng);
      else geoSelect(t.id, false);
    });
    mk.on("dragend", () => {
      const ll = mk.getLatLng();
      geoUpdate(t.id, { lat: ll.lat, lon: ll.lng });
    });
    geoMarkers[t.id] = mk;
  }
}

function geoSelect(id, picking) {
  S.geoSelected = id;
  S.geoPicking = picking;
  const t = S.byId[id];
  $("#geo-map").classList.toggle("picking", picking);
  $("#geo-help").innerHTML = picking
    ? `<b>Cliquer sur la carte</b> pour placer <b>${esc(t.name)}</b> (Échap pour annuler).`
    : `<b>${esc(t.name)}</b> : déplacer le marqueur ou utiliser « Placer sur la carte ».`;
  $$("#geo-rows tr").forEach(tr => tr.classList.toggle("sel", Number(tr.dataset.id) === id));
  renderGeoMap();
  if (t.located) geoMap.setView([t.lat, t.lon], Math.max(geoMap.getZoom(), 12));
}

function placePicked(latlng) {
  if (!S.geoPicking || !S.geoSelected) return;
  S.geoPicking = false;
  $("#geo-map").classList.remove("picking");
  geoUpdate(S.geoSelected, { lat: latlng.lat, lon: latlng.lng });
}
geoMap.on("click", e => placePicked(e.latlng));
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && S.geoPicking) {
    S.geoPicking = false;
    $("#geo-map").classList.remove("picking");
    $("#geo-help").textContent = "Placement annulé.";
  }
});

async function geoUpdate(id, body) {
  try {
    busy("Mise à jour de la position…");
    await api(`/api/teams/${id}/location`, { method: "POST", body });
    $("#geo-help").textContent = "Position enregistrée. Recalcul de la matrice en cours…";
  } catch (e) {
    $("#geo-help").innerHTML = `<span class="ko">${esc(e.message)}</span>`;
  } finally {
    busy(null);
  }
  await loadState();
  pollStatus();
}

async function geoAction(path, method) {
  try {
    await api(path, { method });
  } catch (e) {
    $("#geo-help").innerHTML = `<span class="ko">${esc(e.message)}</span>`;
  }
  await loadState();
  pollStatus();
}


// ------------------------------------------------------------------ matrice
function renderMatrix() {
  const table = $("#matrix");
  if (!S.D) { table.innerHTML = `<tr><td class="muted">Matrice indisponible.</td></tr>`; return; }
  const minutes = $("#matrix-kind").value === "minutes";
  const vals = minutes ? S.Mmin : S.D;
  const A = S.V[S.view] || {};
  const teams = [...S.teams];
  if ($("#matrix-by-pool").checked) teams.sort((a, b) => poolIndex(A[a.id]) - poolIndex(A[b.id]) || a.id - b.id);
  const max = Math.max(...vals.flat());
  $("#matrix-source").textContent = `${VERSION_NAMES[S.view]} · source : ${S.matrixSource}` +
    (S.matrixSource !== "osrm" ? " (haversine x 1,3 pour les paires sans itinéraire OSRM)" : "");
  const head = `<thead><tr><th>${minutes ? "min" : "km"}</th>${teams.map(t =>
    `<th title="${esc(t.name)}">${shapeSvg(A[t.id], 9)}<br>${t.id}</th>`).join("")}</tr></thead>`;
  const body = teams.map(a => `<tr><th>${shapeSvg(A[a.id], 10)} ${a.id}. ${esc(shortName(a.name))}</th>${teams.map(b => {
    if (a.id === b.id) return `<td></td>`;
    const v = vals[S.idx[a.id]][S.idx[b.id]];
    const same = A[a.id] === A[b.id];
    const light = 97 - Math.round(45 * v / (max || 1));
    return `<td class="${same ? "same" : ""}" style="background:hsl(215,60%,${light}%)">${fmt(v)}</td>`;
  }).join("")}</tr>`).join("");
  table.innerHTML = head + `<tbody>${body}</tbody>`;
}

// ------------------------------------------------------------------ statut et chargement
let polling = false;
async function pollStatus() {
  if (polling) return;
  polling = true;
  try {
    for (;;) {
      const st = await api("/api/status");
      S.jobRunning = st.job.running;
      renderStatus(st);
      if (!st.job.running) {
        await reloadData();
        break;
      }
      await new Promise(r => setTimeout(r, 1000));
    }
  } finally {
    polling = false;
  }
}

function renderStatus(st) {
  const el = $("#status");
  const j = st.job;
  if (j.running) {
    el.innerHTML = `⏳ ${esc(j.step)} ${j.total ? `(${j.done}/${j.total})` : ""}`;
  } else if (j.error) {
    el.innerHTML = `<span class="err">Erreur : ${esc(j.error)}</span>`;
  } else if (st.matrix_ready) {
    el.innerHTML = `Matrice : <b>${esc(st.matrix_source)}</b>${st.matrix_error ? ` · <span class="err">${esc(st.matrix_error)}</span>` : ""}`;
  } else {
    el.innerHTML = `<span class="err">${esc(st.matrix_error || "Matrice non calculée")}</span>`;
  }
}

async function loadState() {
  const st = await api("/api/state");
  S.teams = st.teams;
  S.byId = Object.fromEntries(st.teams.map(t => [t.id, t]));
  S.matrixReady = st.matrix_ready;
  S.jobRunning = st.job.running;
  S.optimalFresh = st.optimal_fresh;
  applyOptimal(st.optimal);
  renderStatus({ job: st.job, matrix_ready: st.matrix_ready, matrix_source: st.matrix_source, matrix_error: st.matrix_error });
  renderGeo();
  return st;
}

/** Recharge positions, matrice et version optimale (après une tâche de fond). */
async function reloadData() {
  await loadState();
  if (S.matrixReady) {
    await loadMatrix();
  } else {
    S.R = S.H = S.D = null;
  }
  if (!S.V.mine) S.V.mine = { ...(S.V.optimal || S.V.current) };
  if (!S.V[S.view]) S.view = "mine";
  setView(S.view);
  renderAll();
}

async function init() {
  const st = await loadState();
  S.pools = st.pools;
  S.defaultSizes = st.default_pool_sizes;
  S.V.current = Object.fromEntries(st.teams.map(t => [t.id, t.current_pool]));
  $("#time-limit").value = st.default_time_limit;
  const ui = await api("/api/ui-state").catch(() => ({}));
  const known = new Set(st.teams.map(t => t.id));
  const valid = (A) => A && Object.keys(A).length === known.size &&
    Object.entries(A).every(([k, v]) => known.has(Number(k)) && S.pools.includes(v));
  const savedMine = toIntKeys(ui.mine || ui.assignment);  // ui.assignment : ancien format
  S.V.mine = valid(savedMine) ? savedMine : null;
  if (S.V.mine && Array.isArray(ui.history)) {
    S.history = ui.history.filter(e => valid(toIntKeys(e.before)) && valid(toIntKeys(e.after)))
      .map(e => ({ ...e, before: toIntKeys(e.before), after: toIntKeys(e.after) }));
    S.hpos = Math.min(ui.hpos ?? S.history.length, S.history.length);
    S.lastChange = ui.lastChange || null;
  }
  S.locks = new Set((ui.locks || []).filter(id => known.has(id)));
  S.pairs = (ui.pairs || []).filter(p => known.has(p.a) && known.has(p.b));
  S.settings = ui.settings || {};
  S.linkMode = ui.linkMode || "none";
  $("#link-mode").value = S.linkMode;
  if (ui.labels === false) $("#show-labels").checked = false;
  S.view = ui.view || "mine";
  applySettings(S.settings);
  renderPoolSizes(S.settings.pool_sizes);
  renderPairs();
  await reloadData();
  if (st.job.running || !st.matrix_ready) pollStatus();
}

// ------------------------------------------------------------------ événements
$$(".tab").forEach(b => b.addEventListener("click", () => {
  $$(".tab").forEach(x => x.classList.toggle("active", x === b));
  $$(".tab-panel").forEach(p => p.classList.toggle("active", p.id === `tab-${b.dataset.tab}`));
  if (b.dataset.tab === "geocodage") setTimeout(() => {
    geoMap.invalidateSize();
    if (!geoFitted) {
      const pts = S.teams.filter(t => t.located).map(t => [t.lat, t.lon]);
      if (pts.length) { geoMap.fitBounds(pts, { padding: [20, 20] }); geoFitted = true; }
    }
  }, 50);
  if (b.dataset.tab === "repartition") setTimeout(() => map.invalidateSize(), 50);
  if (b.dataset.tab === "matrice") renderMatrix();
}));
$$("#view-seg button").forEach(b => b.addEventListener("click", () => { setView(b.dataset.view); renderAll(); }));
$("#link-mode").addEventListener("change", e => { S.linkMode = e.target.value; renderMap(); saveUi(); });
$("#show-labels").addEventListener("change", () => { renderMap(); saveUi(); });
$("#btn-optimize").addEventListener("click", optimize);
$("#btn-undo").addEventListener("click", undo);
$("#btn-redo").addEventListener("click", redo);
document.addEventListener("keydown", e => {
  if (e.target.closest("input, select, textarea") || $("#swap-dialog").open) return;
  const k = e.key.toLowerCase();
  if ((e.ctrlKey || e.metaKey) && k === "z" && !e.shiftKey) { e.preventDefault(); undo(); }
  if ((e.ctrlKey || e.metaKey) && (k === "y" || (k === "z" && e.shiftKey))) { e.preventDefault(); redo(); }
});
$("#btn-add-pair").addEventListener("click", () => {
  const a = Number($("#pair-a").value), b = Number($("#pair-b").value), type = $("#pair-type").value;
  if (!a || !b || a === b) { setMessage("Choisir deux équipes différentes.", "err"); return; }
  S.pairs = S.pairs.filter(p => !((p.a === a && p.b === b) || (p.a === b && p.b === a)));
  S.pairs.push({ a, b, type });
  renderPairs();
  saveUi();
});
for (const id of ["#metric", "#stability", "#fairness", "#separate-clubs", "#time-limit"]) {
  $(id).addEventListener("input", () => { renderSliders(); saveUi(); });
}
$$("[data-export]").forEach(b => b.addEventListener("click", () => doExport(b)));
$("#show-all-geo").addEventListener("change", renderGeo);
$("#btn-geocode").addEventListener("click", () => api("/api/geocode/run", { method: "POST" }).then(pollStatus).catch(e => alert(e.message)));
$("#btn-matrix").addEventListener("click", () => api("/api/matrix/refresh", { method: "POST" }).then(pollStatus).catch(e => alert(e.message)));
$("#matrix-kind").addEventListener("change", renderMatrix);
$("#matrix-by-pool").addEventListener("change", renderMatrix);

init().catch(e => { $("#status").innerHTML = `<span class="err">${esc(e.message)}</span>`; console.error(e); });
