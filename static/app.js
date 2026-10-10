"use strict";

const POOL_COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b"];

const S = {
  teams: [],
  pools: [],
  byId: {},
  assignment: {},        // id -> poule affichée
  current: {},           // id -> poule actuelle (CSV)
  locks: new Set(),
  pairs: [],             // {a, b, type: imposed|forbidden}
  settings: {},
  best: null,            // {assignment, metrics, metric}
  metrics: null,         // métriques de la répartition affichée
  currentMetrics: null,  // métriques des poules actuelles
  matrixReady: false,
  selected: null,
  geoSelected: null,
  geoPicking: false,
};

// ------------------------------------------------------------------ utils
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const fmt = (x, d = 0) => (x == null || Number.isNaN(x)) ? "-" :
  Number(x).toLocaleString("fr-FR", { minimumFractionDigits: d, maximumFractionDigits: d });
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const poolColor = (p) => POOL_COLORS[Math.max(0, S.pools.indexOf(p)) % POOL_COLORS.length];
const shortName = (n) => n.replace(/\s+-\s+\d+$/, "").slice(0, 28);

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
  if (opts.raw) return res;
  return res.json();
}

function busy(text) {
  $("#busy").classList.toggle("hidden", !text);
  $("#busy-text").textContent = text || "";
}

let saveTimer = null;
function saveUi() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    api("/api/ui-state", {
      method: "PUT",
      body: { assignment: S.assignment, locks: [...S.locks], pairs: S.pairs, settings: readSettings(), best: S.best },
    }).catch(() => {});
  }, 400);
}

// ------------------------------------------------------------------ cartes
const map = L.map("map", { preferCanvas: false }).setView([48.7, 5.9], 8);
const geoMap = L.map("geo-map").setView([48.7, 5.9], 8);
for (const m of [map, geoMap]) {
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18, attribution: '&copy; contributeurs <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(m);
}
const markers = {};
const routeLayer = L.layerGroup().addTo(map);
const geoLayer = L.layerGroup().addTo(geoMap);
let fitted = false;

function renderMap() {
  const showLabels = $("#show-labels").checked;
  const bounds = [];
  for (const t of S.teams) {
    let mk = markers[t.id];
    if (!t.located) {
      if (mk) { mk.remove(); delete markers[t.id]; }
      continue;
    }
    const pool = S.assignment[t.id];
    const color = poolColor(pool);
    // Décalage léger pour les équipes partageant une commune.
    const twins = S.teams.filter(u => u.located && u.lat === t.lat && u.lon === t.lon);
    const k = twins.indexOf(t);
    const ll = [t.lat + (k ? 0.012 * k : 0), t.lon + (k ? 0.018 * k : 0)];
    if (!mk) {
      mk = L.circleMarker(ll, { radius: 8, weight: 2 }).addTo(map);
      mk.on("click", () => selectTeam(t.id));
      markers[t.id] = mk;
    }
    mk.setLatLng(ll);
    mk.setStyle({ color: "#fff", fillColor: color, fillOpacity: 0.95, weight: S.selected === t.id ? 4 : 2 });
    mk.unbindTooltip();
    const r = S.metrics?.teams.find(x => x.id === t.id);
    const tip = `<b>${esc(t.name)}</b><br>${esc(t.commune || "?")} · poule ${esc(pool)}` +
      (r ? `<br>${fmt(r.km)} km / saison` : "");
    if (showLabels) {
      mk.bindTooltip(esc(shortName(t.name)), { permanent: true, direction: "right", offset: [8, 0], className: "lbl" });
      mk.bindPopup(tip);
    } else {
      mk.unbindPopup();
      mk.bindTooltip(tip);
    }
    bounds.push(ll);
  }
  if (!fitted && bounds.length) { map.fitBounds(bounds, { padding: [20, 20] }); fitted = true; }
  renderRoutes();
}

function renderRoutes() {
  routeLayer.clearLayers();
  const t = S.byId[S.selected];
  if (!t || !t.located) return;
  const pool = S.assignment[t.id];
  for (const u of S.teams) {
    if (u.id === t.id || !u.located || S.assignment[u.id] !== pool) continue;
    L.polyline([[t.lat, t.lon], [u.lat, u.lon]], { color: poolColor(pool), weight: 2, opacity: 0.6, dashArray: "4 4" })
      .addTo(routeLayer);
  }
}

function selectTeam(id) {
  S.selected = S.selected === id ? null : id;
  $$(".team").forEach(el => el.classList.toggle("selected", Number(el.dataset.id) === S.selected));
  renderMap();
  if (S.selected) {
    const el = $(`.team[data-id="${S.selected}"]`);
    if (el) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

// ------------------------------------------------------------------ poules
function renderPools() {
  const box = $("#pools");
  box.style.setProperty("--n", S.pools.length);
  box.innerHTML = "";
  const sizes = readSettings().pool_sizes;
  const byTeam = Object.fromEntries((S.metrics?.teams || []).map(r => [r.id, r]));
  const worstId = S.metrics?.worst_team?.id;
  for (const p of S.pools) {
    const pm = S.metrics?.pools.find(x => x.pool === p);
    const members = S.teams.filter(t => S.assignment[t.id] === p)
      .sort((a, b) => (byTeam[b.id]?.km ?? 0) - (byTeam[a.id]?.km ?? 0) || a.id - b.id);
    const col = document.createElement("div");
    col.className = "pool";
    col.style.setProperty("--c", poolColor(p));
    col.dataset.pool = p;
    const sizeOk = members.length === sizes[p];
    col.innerHTML = `
      <div class="pool-head">
        <h3><span>Poule ${esc(p)}</span><span class="count ${sizeOk ? "" : "bad"}">${members.length} / ${sizes[p]}</span></h3>
        <div class="stats">${pm ? `${fmt(pm.km)} km cumulés · ${fmt(pm.avg_km)} km/équipe · ${fmt(pm.hours)} h` : "matrice en attente"}
        ${pm?.longest_trip ? `<br>Plus long : ${esc(shortName(pm.longest_trip.from))} → ${esc(shortName(pm.longest_trip.to))} (${fmt(pm.longest_trip.km)} km)` : ""}</div>
      </div>`;
    for (const t of members) col.appendChild(teamCard(t, byTeam[t.id], t.id === worstId));
    col.addEventListener("dragover", e => { e.preventDefault(); col.classList.add("over"); });
    col.addEventListener("dragleave", () => col.classList.remove("over"));
    col.addEventListener("drop", e => {
      e.preventDefault();
      col.classList.remove("over");
      const id = Number(e.dataTransfer.getData("text/plain"));
      if (id && S.assignment[id] !== p) moveTeam(id, p);
    });
    box.appendChild(col);
  }
}

function teamCard(t, r, worst) {
  const el = document.createElement("div");
  el.className = "team" + (worst ? " worst" : "") + (S.selected === t.id ? " selected" : "");
  el.draggable = true;
  el.dataset.id = t.id;
  const moved = S.assignment[t.id] !== t.current_pool;
  const delta = (S.best && r) ? r.km - (S.best.metrics.teams.find(x => x.id === t.id)?.km ?? r.km) : null;
  el.innerHTML = `
    <label class="lock" title="Verrouiller dans cette poule"><input type="checkbox" ${S.locks.has(t.id) ? "checked" : ""}>🔒</label>
    <div>
      <div class="name" title="${esc(t.name)}">${t.id}. ${esc(t.name)}</div>
      <div class="sub">${esc(t.commune || (t.located ? "point placé à la main" : "commune inconnue"))}${t.located ? "" : " · ⚠ non localisée"}
        ${moved ? `<span class="moved">· vient de ${esc(t.current_pool)}</span>` : ""}</div>
    </div>
    <div class="km">${r ? `${fmt(r.km)} km` : "-"}
      ${r ? `<small>${fmt(r.hours, 1)} h${delta != null && Math.abs(delta) >= 0.5 ? ` · <span class="delta ${delta > 0 ? "pos" : "neg"}">${delta > 0 ? "+" : ""}${fmt(delta)}</span>` : ""}</small>` : ""}
    </div>`;
  el.querySelector("input").addEventListener("change", e => {
    e.target.checked ? S.locks.add(t.id) : S.locks.delete(t.id);
    saveUi();
  });
  el.querySelector(".lock").addEventListener("click", e => e.stopPropagation());
  el.addEventListener("click", () => selectTeam(t.id));
  el.addEventListener("dragstart", e => { e.dataTransfer.setData("text/plain", String(t.id)); el.classList.add("dragging"); });
  el.addEventListener("dragend", () => el.classList.remove("dragging"));
  return el;
}

async function moveTeam(id, pool) {
  S.assignment[id] = pool;
  await refresh();
}

// ------------------------------------------------------------------ indicateurs
function kpi(label, value, extra = "", wide = false) {
  return `<div class="kpi ${wide ? "wide" : ""}"><div class="v">${value}</div><div class="l">${label}${extra}</div></div>`;
}

function deltaHtml(v, unit = "km", d = 0) {
  if (v == null) return "-";
  const cls = v > 0.05 ? "pos" : v < -0.05 ? "neg" : "";
  return `<span class="delta ${cls}">${v > 0 ? "+" : ""}${fmt(v, d)} ${unit}</span>`;
}

function renderIndicators() {
  const m = S.metrics;
  const box = $("#indicators");
  if (!m) {
    box.innerHTML = `<p class="muted">${S.matrixReady ? "Calcul…" : "Matrice des distances indisponible : voir le statut en haut à droite et l'onglet Géocodage."}</p>`;
    return;
  }
  const gap = S.best ? m.total_km - S.best.metrics.total_km : null;
  let html = kpi("km cumulés (toutes poules)", fmt(m.total_km), ` · ${fmt(m.total_hours)} h`);
  html += kpi("écart avec l'optimum", S.best ? deltaHtml(gap) : "-",
    S.best ? ` · optimum ${fmt(S.best.metrics.total_km)} km` : " · lancer « Optimiser »");
  for (const p of m.pools) {
    html += kpi(`poule ${esc(p.pool)} · ${p.size} équipes`, `<span style="color:${poolColor(p.pool)}">${fmt(p.km)} km</span>`,
      ` · ${fmt(p.avg_km)} km/équipe`);
  }
  if (m.pools.length % 2) html += `<div></div>`;
  if (m.worst_team) html += kpi("équipe la plus pénalisée", `${esc(shortName(m.worst_team.name))}`,
    ` · poule ${esc(m.worst_team.pool)} · ${fmt(m.worst_team.km)} km/saison`, true);
  if (m.longest_trip) html += kpi("trajet le plus long (aller)", `${fmt(m.longest_trip.km)} km`,
    ` · ${esc(shortName(m.longest_trip.from))} → ${esc(shortName(m.longest_trip.to))}`, true);
  html += kpi("équipes changeant de poule", m.moved, "", true);
  box.innerHTML = html;
}

function renderCompare() {
  const box = $("#compare");
  const a = S.currentMetrics, b = S.metrics, o = S.best?.metrics;
  if (!a || !b) { box.innerHTML = `<p class="muted">Disponible quand la matrice est calculée.</p>`; return; }
  const rows = [];
  const row = (label, va, vb, vo, d = 0, unit = "km") => rows.push(
    `<tr><td>${label}</td><td>${fmt(va, d)}</td><td>${fmt(vb, d)}</td><td>${deltaHtml(vb - va, unit, d)}</td>` +
    `<td>${o ? fmt(vo, d) : "-"}</td><td>${o ? deltaHtml(vb - vo, unit, d) : "-"}</td></tr>`);
  row("Km cumulés", a.total_km, b.total_km, o?.total_km);
  row("Heures cumulées", a.total_hours, b.total_hours, o?.total_hours, 0, "h");
  for (const p of S.pools) {
    const f = (m) => m?.pools.find(x => x.pool === p);
    row(`Poule ${esc(p)} (km)`, f(a)?.km, f(b)?.km, f(o)?.km);
  }
  row("Km de l'équipe la plus pénalisée", a.worst_team?.km, b.worst_team?.km, o?.worst_team?.km);
  row("Trajet le plus long (km)", a.longest_trip?.km, b.longest_trip?.km, o?.longest_trip?.km);
  row("Équipes changeant de poule", a.moved, b.moved, o?.moved, 0, "");
  box.innerHTML = `<table class="compare"><thead><tr><th></th><th>Avant (poules actuelles)</th><th>Après (affichée)</th>
    <th>Écart</th><th>Dernier optimum</th><th>Affichée - optimum</th></tr></thead><tbody>${rows.join("")}</tbody></table>`;
}

async function refresh() {
  renderPools();
  renderMap();
  if (!S.matrixReady) { S.metrics = S.currentMetrics = null; renderIndicators(); renderCompare(); return; }
  try {
    // Métriques « avant » (poules actuelles) : recalculées quand la matrice change.
    if (!S.currentMetrics) {
      S.currentMetrics = await api("/api/evaluate", { method: "POST", body: { assignment: S.current } });
    }
    S.metrics = await api("/api/evaluate", { method: "POST", body: { assignment: S.assignment } });
  } catch (e) {
    S.metrics = null;
    setMessage(e.message, "err");
  }
  renderPools();
  renderMap();
  renderIndicators();
  renderCompare();
  if ($("#tab-matrice").classList.contains("active")) renderMatrix();
  saveUi();
}

// ------------------------------------------------------------------ réglages
function readSettings() {
  const sizes = {};
  for (const p of S.pools) {
    const inp = $(`#size-${p}`);
    sizes[p] = inp ? Number(inp.value) : (S.settings.pool_sizes?.[p] ?? 12);
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
  const box = $("#pool-sizes");
  const n = S.teams.length;
  const def = S.pools.length ? Math.floor(n / S.pools.length) : 12;
  box.innerHTML = "Équipes par poule : " + S.pools.map((p, k) => {
    const v = saved?.[p] ?? (k < n % S.pools.length ? def + 1 : def);
    return `<label style="flex-direction:row;align-items:center;gap:3px"><b style="color:${poolColor(p)}">${esc(p)}</b>
      <input type="number" id="size-${p}" min="0" max="${n}" value="${v}"></label>`;
  }).join("");
  $$("input", box).forEach(i => i.addEventListener("change", () => { renderPools(); saveUi(); }));
}

function setMessage(text, cls = "") {
  const el = $("#opt-message");
  el.className = "message " + cls;
  el.textContent = text;
}

async function optimize() {
  const s = readSettings();
  const locks = {};
  for (const id of S.locks) locks[id] = S.assignment[id];
  const body = {
    ...s,
    locks,
    forbidden_pairs: S.pairs.filter(p => p.type === "forbidden").map(p => [p.a, p.b]),
    imposed_pairs: S.pairs.filter(p => p.type === "imposed").map(p => [p.a, p.b]),
    hint: S.assignment,
  };
  const t0 = Date.now();
  const timer = setInterval(() => busy(`Optimisation CP-SAT en cours… ${Math.round((Date.now() - t0) / 1000)} s / ${s.time_limit} s`), 250);
  busy(`Optimisation CP-SAT en cours… 0 s / ${s.time_limit} s`);
  try {
    const r = await api("/api/optimize", { method: "POST", body });
    if (!r.assignment) { setMessage(`${r.status} : ${r.message}`, "err"); return; }
    S.assignment = Object.fromEntries(Object.entries(r.assignment).map(([k, v]) => [Number(k), v]));
    S.best = { assignment: { ...S.assignment }, metrics: r.metrics, metric: s.metric };
    const gap = r.gap != null && r.status !== "OPTIMAL" ? ` Écart à la borne : ${fmt(r.gap * 100, 1)} %.` : "";
    setMessage(`${r.message} ${fmt(r.metrics.total_km)} km cumulés en ${fmt(r.wall_time, 1)} s.${gap}`, "ok");
    await refresh();
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
  const name = btn.dataset.export;
  try {
    const res = await api(`/api/export/${name}`, {
      method: "POST", raw: true,
      body: { assignment: S.assignment, reference: S.current, kind: btn.dataset.kind || "distance" },
    });
    const blob = await res.blob();
    const fname = (res.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/)?.[1] || name;
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
let matrixCache = null;
async function renderMatrix() {
  const table = $("#matrix");
  if (!S.matrixReady) { table.innerHTML = `<tr><td class="muted">Matrice indisponible.</td></tr>`; return; }
  if (!matrixCache) matrixCache = await api("/api/matrix");
  const kind = $("#matrix-kind").value;
  const vals = matrixCache[kind];
  const idx = Object.fromEntries(matrixCache.ids.map((id, i) => [id, i]));
  let teams = [...S.teams];
  if ($("#matrix-by-pool").checked) {
    teams.sort((a, b) => S.pools.indexOf(S.assignment[a.id]) - S.pools.indexOf(S.assignment[b.id]) || a.id - b.id);
  }
  const max = Math.max(...vals.flat());
  $("#matrix-source").textContent = `Source : ${matrixCache.source}${matrixCache.source !== "osrm" ? " (haversine x 1,3 pour les paires sans itinéraire OSRM)" : ""}`;
  const head = `<thead><tr><th>${kind === "km" ? "km" : "min"}</th>${teams.map(t =>
    `<th style="color:${poolColor(S.assignment[t.id])}" title="${esc(t.name)}">${t.id}</th>`).join("")}</tr></thead>`;
  const body = teams.map(a => `<tr><th style="color:${poolColor(S.assignment[a.id])}">${a.id}. ${esc(shortName(a.name))}</th>${teams.map(b => {
    if (a.id === b.id) return `<td></td>`;
    const v = vals[idx[a.id]][idx[b.id]];
    const same = S.assignment[a.id] === S.assignment[b.id];
    const light = 96 - Math.round(40 * v / (max || 1));
    return `<td class="${same ? "same" : ""}" style="background:hsl(${same ? 140 : 25},60%,${light}%)">${fmt(v)}</td>`;
  }).join("")}</tr>`).join("");
  table.innerHTML = head + `<tbody>${body}</tbody>`;
}

// ------------------------------------------------------------------ statut
let polling = false;
async function pollStatus() {
  if (polling) return;
  polling = true;
  try {
    for (;;) {
      const st = await api("/api/status");
      renderStatus(st);
      if (!st.job.running) {
        const wasReady = S.matrixReady;
        S.matrixReady = st.matrix_ready;
        matrixCache = null;
        S.currentMetrics = null;
        await loadState();
        if (S.matrixReady || wasReady) await refresh();
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

// ------------------------------------------------------------------ chargement
async function loadState() {
  const st = await api("/api/state");
  S.teams = st.teams;
  S.byId = Object.fromEntries(st.teams.map(t => [t.id, t]));
  S.matrixReady = st.matrix_ready;
  renderStatus({ job: st.job, matrix_ready: st.matrix_ready, matrix_source: st.matrix_source, matrix_error: st.matrix_error });
  renderGeo();
  return st;
}

async function init() {
  const st = await loadState();
  S.pools = st.pools;
  S.current = Object.fromEntries(st.teams.map(t => [t.id, t.current_pool]));
  $("#time-limit").value = st.default_time_limit;
  const ui = await api("/api/ui-state").catch(() => ({}));
  const known = new Set(st.teams.map(t => t.id));
  S.assignment = { ...S.current };
  if (ui.assignment) {
    for (const [k, v] of Object.entries(ui.assignment)) {
      if (known.has(Number(k)) && S.pools.includes(v)) S.assignment[Number(k)] = v;
    }
  }
  S.locks = new Set((ui.locks || []).filter(id => known.has(id)));
  S.pairs = (ui.pairs || []).filter(p => known.has(p.a) && known.has(p.b));
  S.best = ui.best || null;
  S.settings = ui.settings || {};
  applySettings(S.settings);
  renderPoolSizes(S.settings.pool_sizes);
  renderPairs();

  await refresh();
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
$("#show-labels").addEventListener("change", renderMap);
$("#btn-optimize").addEventListener("click", optimize);
$("#btn-reset").addEventListener("click", () => { S.assignment = { ...S.current }; setMessage("Retour aux poules actuelles."); refresh(); });
$("#btn-best").addEventListener("click", () => {
  if (!S.best) { setMessage("Aucune optimisation lancée pour l'instant."); return; }
  S.assignment = { ...S.best.assignment };
  refresh();
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

init().catch(e => { $("#status").innerHTML = `<span class="err">${esc(e.message)}</span>`; });
