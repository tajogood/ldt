/* 가축질병 추적 — 화면 스크립트 (빌드 도구 없이 바로 실행) */
const CFG = window.LDT_CONFIG;
const S = { species: "poultry", view: "dash", maps: {}, charts: {}, selected: null, lang: "ko" };
try { S.lang = localStorage.getItem("ldt.lang") || "ko"; } catch (_) { /* 저장소 없음: 기본값 */ }

const COLOR = { outbreak: "#B42318", wild_detection: "#1D4ED8", movement_restriction: "#B54708",
  surveillance_positive: "#7A5AF8", alert: "#B42318", farm: "#17201B", disinfection: "#0E5A55",
  slaughterhouse: "#6B4E16", ai_center: "#56605A", wintering_site: "#1D4ED8" };
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = async (path, opt) => { const r = await fetch(path, opt); if (!r.ok) throw new Error((await r.json()).detail || r.status); return r.json(); };
const ageAlpha = (d) => { const days = (Date.now() - Date.parse(d)) / 864e5; return days <= 7 ? 1 : days <= 21 ? 0.7 : 0.35; };

/* ── 언어 ─────────────────────────────────────────────────────────────── */
function t(key, vars = {}) {
  const s = (I18N[S.lang] && I18N[S.lang][key]) ?? I18N.ko[key] ?? key;
  return s.replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? "");
}
const kindLabel = (k) => t("kind." + k);
const spLabel = (k) => t("spc." + k);
const fmtTime = (iso) => iso ? new Date(iso).toLocaleString(S.lang === "en" ? "en-GB" : "ko-KR",
  { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) : "";
const fmtNum = (n) => Number(n).toLocaleString(S.lang === "en" ? "en-US" : "ko-KR");

function applyLang() {
  document.documentElement.lang = S.lang;
  document.title = t("app.title");
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
    if (I18N.ko["tip." + el.dataset.i18n]) el.dataset.tip = "tip." + el.dataset.i18n;   // 설명이 있는 항목은 툴팁
  });
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => { el.placeholder = t(el.dataset.i18nPh); });
  document.querySelectorAll(".lang button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.lang === S.lang));
  $("#f-kind").innerHTML = ["outbreak", "wild_detection", "movement_restriction", "surveillance_positive", "alert"]
    .map((k) => `<option value="${k}">${kindLabel(k)}</option>`).join("");
  $("#f-species").innerHTML = ["poultry", "pig", "cattle", "wild_bird", "wild_boar"]
    .map((k) => `<option value="${k}">${spLabel(k)}</option>`).join("");
}
document.querySelectorAll(".lang button").forEach((b) => b.onclick = () => {
  S.lang = b.dataset.lang;
  try { localStorage.setItem("ldt.lang", S.lang); } catch (_) { /* 무시 */ }
  applyLang(); refresh();
});

/* ── 데이터 최신성 표시 ───────────────────────────────────────────────── */
function freshHTML(list) {
  if (!list || !list.length) return `<span class="muted">${t("fr.none")}</span>`;
  return `<span class="fr-label">${t("fr.label")}</span>` + list.map((f) => {
    const name = S.lang === "en" ? f.title_en : f.title;
    const period = f.data_to ? (f.data_from && f.data_from !== f.data_to ? t("fr.range", { a: f.data_from, b: f.data_to }) : t("fr.to", { d: f.data_to })) : "";
    const stale = f.stale ? ` <b class="stale">${t("fr.stale")}</b>` : "";
    return `<span class="fr-item" title="${esc(f.source)} · ${fmtNum(f.rows)}"><b>${esc(name)}</b> ${period}${period ? " · " : ""}${t("fr.collected", { t: fmtTime(f.collected_at) })}${stale}</span>`;
  }).join("");
}
const showFresh = (id, list) => { const el = document.getElementById(id); if (el) el.innerHTML = freshHTML(list); };

async function lastCollect() {
  const [all, auto, ver] = await Promise.all([api("/api/freshness"), api("/api/auto"), api("/api/version").catch(() => null)]);
  S.auto = auto;
  const ts = all.map((f) => f.collected_at).filter(Boolean).sort().pop();
  const stale = all.filter((f) => f.stale).length;
  $("#last-collect").innerHTML = (ts ? t("fr.last", { t: fmtTime(ts) }) : t("fr.never")) +
    (stale ? ` · <b class="stale">${t("fr.stale")} ${stale}</b>` : "") +
    (auto.running ? ` · <b>${t("auto.running", { k: t("auto." + auto.running), t: fmtTime(auto.started_at) })}</b>` : auto.enabled ? ` · ${t("auto.bar", { at: auto.at })}` : "") +
    (ver ? ` · <span data-tip="tip.ver">${t("ver", { c: ver.code, t: fmtTime(ver.started_at) })}</span>` +
      (ver.disk && ver.disk !== ver.code ? ` <b class="stale">${t("ver.old", { d: ver.disk })}</b>` : "") : "");
}

/* ── 툴팁: data-tip="tip.키" 또는 문장 — 마우스를 올리거나 키보드로 초점을 주면 설명 ── */
const TIP = document.createElement("div"); TIP.id = "tip"; TIP.setAttribute("role", "tooltip"); TIP.hidden = true; document.body.appendChild(TIP);
const tipText = (v) => v && v.startsWith("tip.") ? t(v) : v;
function showTip(el) {
  const txt = tipText(el.dataset.tip); if (!txt || txt === el.dataset.tip) return;
  TIP.textContent = txt; TIP.hidden = false;
  const r = el.getBoundingClientRect(), w = Math.min(340, window.innerWidth - 24);
  TIP.style.maxWidth = w + "px";
  const left = Math.max(12, Math.min(window.innerWidth - TIP.offsetWidth - 12, r.left + r.width / 2 - TIP.offsetWidth / 2));
  const below = r.bottom + 8 + TIP.offsetHeight < window.innerHeight;
  TIP.style.left = left + "px"; TIP.style.top = (below ? r.bottom + 8 : r.top - TIP.offsetHeight - 8) + "px";
}
document.addEventListener("mouseover", (e) => { const el = e.target.closest?.("[data-tip]"); if (el) showTip(el); else TIP.hidden = true; });
document.addEventListener("focusin", (e) => { const el = e.target.closest?.("[data-tip]"); if (el) showTip(el); });
document.addEventListener("scroll", () => { TIP.hidden = true; }, true);
const tipA = (key) => I18N.ko["tip." + key] ? ` data-tip="tip.${key}"` : "";

/* ── 지도 어댑터: 국토교통부 브이월드(VWorld) WMTS + Leaflet, 키가 없으면 단순 SVG 대체 지도 ── */
async function makeMap(el, opts = {}) { return (CFG.vworldKey && window.L) ? vworldMap(el, opts) : svgMap(el); }

function vworldMap(el, opts = {}) {
  const key = encodeURIComponent(CFG.vworldKey);
  const url = (layer, ext) => `https://api.vworld.kr/req/wmts/1.0.0/${key}/${layer}/{z}/{y}/{x}.${ext}`;
  const opt = (maxZoom) => ({ minZoom: 6, maxZoom, zIndex: 1, attribution: '&copy; <a href="https://www.vworld.kr" target="_blank" rel="noopener">VWorld</a> 국토교통부' });
  const baseLayer = L.tileLayer(url("Base", "png"), opt(19));
  const bases = {
    [t("map.base")]: baseLayer,
    [t("map.gray")]: L.tileLayer(url("gray", "png"), opt(18)),
    [t("map.sat")]: L.layerGroup([L.tileLayer(url("Satellite", "jpeg"), opt(19)), L.tileLayer(url("Hybrid", "png"), opt(19))]),
  };
  const map = L.map(el, { center: [36.3, 127.8], zoom: opts.zoom || 7, minZoom: 4, maxZoom: 19 });
  // 한반도 밖(철새 경로 등)을 볼 때 쓰는 바탕: OpenStreetMap — 브이월드 타일 아래에 깔림
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { minZoom: 4, maxZoom: 19, opacity: 0.55, zIndex: 0,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>' }).addTo(map);
  (opts.base === "gray" ? bases[t("map.gray")] : baseLayer).addTo(map);
  L.control.layers(bases, null, { position: "topright" }).addTo(map);
  L.control.scale({ imperial: false }).addTo(map);
  const R = L.canvas({ padding: 0.3, tolerance: 4 });   // 모든 도형이 쓰는 단 하나의 캔버스
  const i0 = (items) => Math.max(1, ...items.map((i) => i.z || 1));
  // 새로 그린 레이어보다 위에 있어야 할 레이어만 다시 맨 위로(재생 중 매 프레임 비용을 줄이려고)
  const restack = (added) => Object.values(layers).filter((g) => g && g !== added && g.z > added.z).sort((a, b) => a.z - b.z)
    .forEach((g) => g.eachLayer((l) => l.bringToFront && l.bringToFront()));

  // 키가 잘못됐거나 서비스 URL이 등록되지 않으면 타일이 전부 실패한다 → 안내 표시
  let ok = 0, fail = 0, shown = false;
  baseLayer.on("tileload", () => { ok++; });
  baseLayer.on("tileerror", () => {
    fail++;
    if (!shown && ok === 0 && fail >= 4) {
      shown = true;
      const n = document.createElement("div"); n.className = "notice err"; n.textContent = t("map.auth"); el.appendChild(n);
    }
  });

  const layers = {};
  return {
    // 모든 도형을 한 캔버스에 그린다(캔버스가 여러 장이면 위 캔버스가 아래 도형의 마우스오버를 가로챔).
    // 그리는 순서: 경계면(0) → 농장·방역대(1) → 발생 점(5). 점 레이어를 새로 그릴 때마다 맨 위로 올린다.
    dots(name, items, onClick) {
      layers[name]?.remove();
      const g = L.featureGroup();
      items.filter((i) => i.lat != null).forEach((i) => {
        const mk = L.circleMarker([i.lat, i.lon], { renderer: R, radius: i.r, color: i.stroke || "#fff", weight: i.weight ?? (i.square ? 1 : 2),
          fillColor: i.color, fillOpacity: i.opacity ?? 1, interactive: i.interactive ?? true });
        if (i.title) mk.bindTooltip(esc(i.title), { sticky: true, direction: "top" });
        if (onClick) mk.on("click", () => onClick(i));
        g.addLayer(mk);
      });
      g.z = i0(items);
      layers[name] = g.addTo(map);
      restack(g);
    },
    circles(name, items) {
      layers[name]?.remove();
      const g = L.featureGroup(items.map((c) => {
        const ci = L.circle([c.lat, c.lon], { renderer: R, radius: c.km * 1000, color: c.color, weight: 2, dashArray: c.dashed ? "6 6" : null,
          fillColor: c.color, fillOpacity: c.fill ?? 0.06, interactive: !!c.title });
        if (c.title) ci.bindTooltip(esc(c.title), { sticky: true });
        return ci;
      }));
      g.z = 1;
      layers[name] = g.addTo(map);
      restack(g);
    },
    lines(name, segs) {
      layers[name]?.remove();
      const g = L.featureGroup(segs.map((s) => {
        const ln = L.polyline([s.from, s.to], { renderer: R, color: s.color, weight: s.title ? 2.5 : 1.5, opacity: s.opacity ?? 0.7, interactive: !!s.title });
        if (s.title) ln.bindTooltip(esc(s.title), { sticky: true });
        return ln;
      }));
      g.z = 3;
      layers[name] = g.addTo(map);
      restack(g);
    },
    // 면(GeoJSON): style(props) → {fillColor, fillOpacity, ...}, tip(props) → 툴팁 HTML
    polys(name, fc, style, tip) {
      layers[name]?.remove();
      if (!fc?.features?.length) { delete layers[name]; return; }
      const g = L.geoJSON(fc, { renderer: R, style: (f) => ({ color: "#fff", weight: 0.6, opacity: 0.9, fillOpacity: 0.6, ...style(f.properties) }),
        onEachFeature: (f, l) => {
          if (tip) l.bindTooltip(tip(f.properties), { sticky: true });
          l.on("mouseover", () => l.setStyle({ weight: 2, color: "#17201B" }));
          l.on("mouseout", () => l.setStyle({ weight: 0.6, color: "#fff" }));
        } });
      g.z = 0;
      layers[name] = g.addTo(map);
      restack(g);
    },
    // 화살표(곡선 + 머리): 철새 이동 같은 출발→도착 벡터
    arrows(name, items) {
      layers[name]?.remove();
      const g = L.featureGroup();
      items.forEach((a) => {
        const [y1, x1] = a.from, [y2, x2] = a.to;
        const dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy);
        if (!len) return;
        const bend = (a.bend ?? 0.12) * len, mx = (x1 + x2) / 2 - dy / len * bend, my = (y1 + y2) / 2 + dx / len * bend;
        const pts = [];
        for (let i = 0; i <= 24; i++) { const u = i / 24;
          pts.push([(1 - u) * (1 - u) * y1 + 2 * (1 - u) * u * my + u * u * y2, (1 - u) * (1 - u) * x1 + 2 * (1 - u) * u * mx + u * u * x2]); }
        const ln = L.polyline(pts, { renderer: R, color: a.color, weight: a.weight || 2, opacity: a.opacity ?? 0.85 });
        // 머리: 마지막 구간 방향으로 작은 삼각형
        const [py, px] = pts[22], hx = x2 - px, hy = y2 - py, hl = Math.hypot(hx, hy) || 1;
        const size = Math.min(0.9, Math.max(0.18, len * 0.06)), ux = hx / hl, uy = hy / hl;
        const head = L.polygon([[y2, x2], [y2 - uy * size + ux * size * 0.45, x2 - ux * size - uy * size * 0.45],
          [y2 - uy * size - ux * size * 0.45, x2 - ux * size + uy * size * 0.45]],
          { renderer: R, color: a.color, weight: 1, fillColor: a.color, fillOpacity: a.opacity ?? 0.85 });
        if (a.title) { ln.bindTooltip(esc(a.title), { sticky: true }); head.bindTooltip(esc(a.title), { sticky: true }); }
        g.addLayer(ln); g.addLayer(head);
        if (a.start) g.addLayer(L.circleMarker(a.from, { renderer: R, radius: 3, color: "#fff", weight: 1, fillColor: a.color, fillOpacity: 1 }));
      });
      g.z = 3;
      layers[name] = g.addTo(map);
      restack(g);
    },
    fitLatLngs(pts, maxZoom = 7) { if (pts.length) map.fitBounds(L.latLngBounds(pts), { padding: [20, 20], maxZoom }); },
    fitPoints(pts) { if (pts.length) map.fitBounds(L.latLngBounds(pts.map((p) => [p.lat, p.lon])), { padding: [30, 30], maxZoom: 10 }); },
    // zoom: 숫자가 클수록 확대 (10km 방역대가 한 화면에 들어오는 값 ≈ 11)
    focus(lat, lon, zoom = 11) { map.setView([lat, lon], zoom); },
    onIdle(fn) {
      const emit = () => { const b = map.getBounds(); fn({ zoom: map.getZoom(), bbox: [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()] }); };
      map.on("moveend", emit);
    },
    view() { const b = map.getBounds(); return { zoom: map.getZoom(), bbox: [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()] }; },
    relayout() { map.invalidateSize(); },
  };
}

function svgMap(el) {
  // 지도 키가 없을 때 개발용: 위경도를 그대로 평면에 찍는 단순 지도
  el.innerHTML = `<svg viewBox="0 0 600 700" width="100%" height="100%" role="img" aria-label="map"></svg><div class="notice" data-i18n="fallback">${t("fallback")}</div>`;
  const svg = el.querySelector("svg");
  let view = { lat0: 33.0, lat1: 38.7, lon0: 125.0, lon1: 130.0 };
  const X = (lon) => ((lon - view.lon0) / (view.lon1 - view.lon0)) * 600;
  const Y = (lat) => 700 - ((lat - view.lat0) / (view.lat1 - view.lat0)) * 700;
  const kmPx = () => 600 / ((view.lon1 - view.lon0) * 91);
  const layers = {}, items = {}, cbs = {}, circ = {};
  const draw = () => {
    svg.innerHTML = Object.values(layers).join("");
    svg.querySelectorAll("[data-i]").forEach((n) => { const [name, i] = n.dataset.i.split("|"); if (cbs[name]) n.onclick = () => cbs[name](items[name][+i]); });
  };
  const m = {
    polys() {},   // 대체 지도에서는 경계면을 그리지 않음
    arrows() {}, fitLatLngs() {},
    dots(name, list, onClick) {
      items[name] = list.filter((i) => i.lat != null); cbs[name] = onClick;
      layers[name] = items[name].map((i, k) => `<circle data-i="${name}|${k}" cx="${X(i.lon)}" cy="${Y(i.lat)}" r="${i.r}" fill="${i.color}" fill-opacity="${i.opacity ?? 1}" stroke="#fff" style="cursor:pointer"><title>${esc(i.title)}</title></circle>`).join("");
      draw();
    },
    lines(name, segs) { layers[name] = segs.map((g) => `<line x1="${X(g.from[1])}" y1="${Y(g.from[0])}" x2="${X(g.to[1])}" y2="${Y(g.to[0])}" stroke="${g.color}" stroke-opacity="${g.opacity ?? 0.7}"/>`).join(""); draw(); },
    fitPoints() {},
    circles(name, list) { circ[name] = list; layers[name] = list.map((c) => `<circle cx="${X(c.lon)}" cy="${Y(c.lat)}" r="${c.km * kmPx()}" fill="${c.color}" fill-opacity="${c.fill ?? 0.06}" stroke="${c.color}" stroke-dasharray="${c.dashed ? "6 4" : ""}"/>`).join(""); draw(); },
    focus(lat, lon) { view = { lat0: lat - 0.35, lat1: lat + 0.35, lon0: lon - 0.4, lon1: lon + 0.4 };
      Object.keys(items).forEach((n) => m.dots(n, items[n], cbs[n])); Object.keys(circ).forEach((n) => m.circles(n, circ[n])); },
    onIdle() {}, relayout() {},
  };
  return m;
}

/* ── 공통 ─────────────────────────────────────────────────────────────── */
function renderSpeciesTabs() {
  const tabs = [...Object.entries(CFG.species).map(([k, v]) => [k, S.lang === "en" ? v.label_en : v.label]), ["all", t("sp.all")]];
  $("#species").innerHTML = tabs.map(([k, v]) => `<button role="tab" aria-selected="${k === S.species}" data-sp="${k}">${esc(v)}</button>`).join("");
  $("#species").querySelectorAll("button").forEach((b) => b.onclick = () => { S.species = b.dataset.sp; refresh(); });
  const c = CFG.species[S.species];
  const ds = c ? (S.lang === "en" ? c.diseases_en : c.diseases) : {};
  $("#diseases").innerHTML = Object.values(ds).map((d) => `<span>${esc(d)}</span>`).join("");
}

document.querySelectorAll("#nav button").forEach((b) => b.onclick = () => {
  S.view = b.dataset.view;
  document.querySelectorAll("#nav button").forEach((x) => x.classList.toggle("on", x === b));
  document.querySelectorAll(".view").forEach((v) => v.hidden = v.id !== "view-" + S.view);
  refresh();
});

function chart(id, cfg) {
  if (!window.Chart) return;
  try { S.charts[id]?.destroy(); } catch (_) { /* 이미 정리됨 */ }
  delete S.charts[id];
  const el = document.getElementById(id); if (!el) return;
  Chart.getChart(el)?.destroy();           // 그리다 실패해 남은 차트가 있으면 정리
  try {
    S.charts[id] = new Chart(el, { ...cfg, options: { maintainAspectRatio: false, responsive: true,
      plugins: { legend: { position: "bottom" } }, ...cfg.options } });
  } catch (e) { console.warn("chart", id, e); }
}
const clearChart = (id) => { try { S.charts[id]?.destroy(); } catch (_) {} delete S.charts[id]; };

const place = (e) => (e.place && e.sido && e.place.startsWith(e.sido)) ? e.place : [e.sido, e.sigungu, e.place].filter(Boolean).join(" ");
const evDot = (e) => ({ ...e, r: e.kind === "outbreak" ? 7 : 6, color: COLOR[e.kind] || "#555", opacity: ageAlpha(e.date), z: 5,
  title: `${e.date} ${kindLabel(e.kind)} ${e.disease || ""} ${e.place || e.sigungu || ""}` });
const FACTORS = ["density", "large_share", "wild_proximity", "recent_outbreak", "contact", "weather"];

/* ── 1. 통합 상황판 ───────────────────────────────────────────────────── */
const DASH_LAYERS = ["farms", "farmpts", "visits", "wx", "birds", "cur", "prev", "zones", "disinf"];
const DASH_DEFAULT = ["farms", "farmpts", "visits", "wx", "cur", "prev", "zones", "disinf"];
const dashOn = (() => { try { const v = JSON.parse(localStorage.getItem("ldt.dash") || "null"); return new Set(v || DASH_DEFAULT); }
  catch { return new Set(DASH_DEFAULT); } })();
const seasonRange = (sp, back = 0) => {
  const sm = CFG.species[sp]?.season_start_month || 1, now = new Date();
  const y = (now.getMonth() + 1 >= sm ? now.getFullYear() : now.getFullYear() - 1) - back;
  const from = `${y}-${String(sm).padStart(2, "0")}-01`;
  const end = new Date(y + 1, sm - 1, 0);   // 다음 시즌 시작 전날
  return { from, to: `${end.getFullYear()}-${String(end.getMonth() + 1).padStart(2, "0")}-${String(end.getDate()).padStart(2, "0")}`,
    label: sm > 1 ? `${y}/${String(y + 1).slice(2)}` : String(y) };
};

async function viewDash() {
  const sp = S.species === "all" ? "poultry" : S.species;
  const cur = seasonRange(sp), prev = seasonRange(sp, 1);
  const [sum, risk, series, dens, evCur, evPrev, disinf] = await Promise.all([
    api(`/api/summary?species=${S.species}`), api(`/api/risk?species=${sp}`), api(`/api/series?species=${S.species}`),
    api(`/api/farms/density?species=${S.species}`),
    api(`/api/events?species=${S.species}&from=${cur.from}`),
    api(`/api/events?species=${S.species}&from=${prev.from}&to=${prev.to}`),
    api(`/api/facilities?kind=disinfection`)]);
  const k = sum.kpi;
  $("#alert").hidden = !sum.alert;
  if (sum.alert) $("#alert").textContent = `${sum.alert.date} · ${(sum.alert.note || kindLabel("alert")).split(".")[0]}`;
  const vsPrev = (n) => k.prev ? ` · ${t("kpi.prev", { n })}` : "";
  $("#kpis").innerHTML = [
    ["kpi.outbreaks", k.outbreaks, t("kpi.since", { d: k.season_start }) + vsPrev(k.prev?.outbreaks), "var(--red)"],
    ["kpi.wild", k.wild_detections, t("kpi.since", { d: k.season_start }) + vsPrev(k.prev?.wild_detections), "var(--blue)"],
    ["kpi.restrict", k.restrictions_active, t("kpi.now"), "var(--amber)"],
    ["kpi.farms", fmtNum(k.farms), k.heads ? t("kpi.headsub", { n: fmtNum(k.heads) }) : t(k.farms_source ? "kpi.farmsub2" : "kpi.farmsub"), "var(--ink)"],
  ].map(([l, v, s, c]) => `<div class="kpi"><span class="muted"${tipA(l)}>${t(l)}</span><b style="color:${c}">${v}</b><span class="muted"${k.prev && l !== "kpi.restrict" && l !== "kpi.farms" ? tipA("kpi.prev") : ""}>${s}</span></div>`).join("");
  showFresh("fr-kpis", sum.freshness);
  dashNews();

  $("#feed").innerHTML = sum.feed.map((e) => `<li data-id="${e.id}"><span class="d">${e.date}</span><span>
    <span class="tag k-${e.kind}">${kindLabel(e.kind)}</span> ${esc(e.disease || "")}<br>${esc(place(e))}</span></li>`).join("")
    || `<li class="muted">${t("dash.nofeed")}</li>`;
  $("#feed").querySelectorAll("li[data-id]").forEach((li) => li.onclick = () => openEvent(+li.dataset.id));

  $("#risk-top").innerHTML = risk.regions.slice(0, 8).map((r, i) => {
    const main = Object.entries(r.contrib).sort((a, b) => b[1] - a[1])[0];
    return `<tr><td>${i + 1}</td><td><b>${esc(r.region)}</b></td><td>${r.score.toFixed(2)}</td><td class="muted">${t("factor." + main[0])}</td></tr>`;
  }).join("") || `<tr><td colspan="4" class="muted">${t("risk.none")}</td></tr>`;
  $("#risk-missing").textContent = risk.missing?.length ? t("risk.missing", { f: risk.missing.map((m) => t("factor." + m)).join(", ") }) : "";
  showFresh("fr-risk", freshMerge(risk.freshness, dens.freshness));

  // ── 현황 지도: 농장 분포(노출) × 이번 시즌 발생 × 직전 시즌(참고) × 활성 방역대 × 거점소독
  const m = S.maps.dash ||= await makeMap($("#map-dash"));
  const today = new Date().toISOString().slice(0, 10);
  const d30 = new Date(Date.now() - 30 * 864e5).toISOString().slice(0, 10);
  const R = CFG.species[sp]?.radii || { control: 3, surveillance: 10 };
  // 농장 레이어: 읍면동 경계(지오펜스)를 색으로 칠한다. 경계가 아직 없으면 읍면동 대표점 원으로 대신(농장 위치 아님).
  S.shapes ||= {};
  if (!S.shapes[S.species]?.features?.length) S.shapes[S.species] = await api(`/api/farms/shapes?species=${S.species}`).catch(() => null);
  const shp = S.shapes[S.species];
  const hasShapes = !!shp?.features?.length;
  const metric = S.dashMetric || (dens.has_heads && S.species !== "all" ? "heads" : "farms");
  const valOf = (d) => metric === "heads" ? d.heads : metric === "avg" ? d.avg : d.farms;
  const SEQ = ["#E3F1E6", "#B3DABE", "#73B98C", "#3A8A5A", "#17502F"];
  const vals = (hasShapes ? shp.features.map((f) => f.properties) : dens.items).map(valOf).filter((v) => v != null && v > 0).sort((x, y) => x - y);
  const brk = vals.length ? [0.2, 0.4, 0.6, 0.8].map((q) => vals[Math.floor(q * (vals.length - 1))]) : [];
  const cls = (v) => v == null ? -1 : brk.filter((b) => v > b).length;
  const tipOf = (d, name) => `<b>${esc(name)}</b><br>${t("dash.farmsN", { n: fmtNum(d.farms) })}` +
    (d.heads != null ? `<br>${t("dash.headsN", { n: fmtNum(d.heads), a: fmtNum(d.avg) })}` : "");
  const fmax = Math.max(1, ...dens.items.map((d) => valOf(d) || 0));
  const active = evCur.items.filter((e) => e.kind === "outbreak" && e.date >= d30 && e.lat != null);
  const layerData = {
    farms: () => {
      if (hasShapes) {
        m.dots("farms", []);
        m.polys("farms-geo", shp, (p) => ({ fillColor: SEQ[Math.max(0, cls(valOf(p)))], fillOpacity: 0.62 }), (p) => tipOf(p, p.name));
      } else {
        m.polys("farms-geo", null);
        m.dots("farms", dens.items.map((d) => ({ ...d, r: 2 + 12 * Math.sqrt((valOf(d) || 0) / fmax), color: SEQ[Math.max(0, cls(valOf(d)))],
          opacity: 0.75, weight: 0.5, stroke: "#fff",
          title: `${d.sido} ${d.sigungu} ${d.emd || ""} — ${t("dash.centroid")} · ${t("dash.farmsN", { n: fmtNum(d.farms) })}` +
            (d.heads != null ? ` · ${t("dash.headsN", { n: fmtNum(d.heads), a: fmtNum(d.avg) })}` : "") })));
      }
    },
    prev: () => m.dots("prev", evPrev.items.filter((e) => ["outbreak", "wild_detection"].includes(e.kind)).map((e) => ({ ...e,
      r: 4, color: e.kind === "outbreak" ? "#9A6B6B" : "#7C8FB8", opacity: 0.45, weight: 0, z: 2,
      title: `${prev.label} · ${e.date} ${kindLabel(e.kind)} ${e.disease || ""} ${e.place || ""}` })), (e) => openEvent(e.id)),
    cur: () => m.dots("cur", evCur.items.map((e) => ({ ...evDot(e), opacity: 0.95,
      r: e.date >= d30 ? 8 : 6, stroke: e.date >= d30 ? "#17201B" : "#fff" })), (e) => openEvent(e.id)),
    zones: () => m.circles("zones", active.flatMap((e) => [
      { lat: e.lat, lon: e.lon, km: R.control, color: "#B42318", fill: 0.10, title: t("dash.zoneTip", { z: t("d.control"), km: R.control, d: e.date, p: e.place || "" }) },
      { lat: e.lat, lon: e.lon, km: R.surveillance, color: "#B54708", fill: 0.03, dashed: true, title: t("dash.zoneTip", { z: t("d.surv"), km: R.surveillance, d: e.date, p: e.place || "" }) }])),
    // 실제 농장 위치(행안부 가축사육업 인허가) — 전국 10만 곳이라 확대(줌 10 이상)했을 때 보이는 범위만 불러온다
    farmpts: () => loadFarmPts(m, S.species),
    wx: () => drawWxMap(m, true),
    birds: () => drawBirds(m, true),
    // 최근 7일 축산차량 방문(리 대표점, 원 크기 = 방문 횟수)
    visits: async () => {
      const vm = await api(`/api/visits/map?species=${S.species}&days=7`).catch(() => ({ items: [] }));
      const vmax = Math.max(1, ...vm.items.map((x) => x.n));
      m.dots("visits", vm.items.map((x) => ({ ...x, r: 2 + 10 * Math.sqrt(x.n / vmax), color: "#7A5AF8", opacity: 0.45, weight: 0, z: 1,
        title: `${x.address} · ${t("visit.n7", { n: fmtNum(x.n) })} · ${x.top.map(([k, n]) => `${k} ${fmtNum(n)}`).join(", ")}` })));
      const h = $("#visit-hint"); if (h) h.textContent = fmtNum(vm.items.length);
    },
    disinf: () => m.dots("disinf", disinf.items.map((f) => ({ ...f, r: 4, color: COLOR.disinfection, opacity: 0.95, weight: 1, square: true, z: 4,
      title: `${t("lyr.disinf")} · ${f.name || ""} · ${f.status || ""} · ${f.address || ""}` }))),
  };
  const counts = { birds: 0, wx: 0, visits: 0, farmpts: 0, farms: hasShapes ? shp.features.length : dens.located, cur: evCur.items.filter((e) => e.lat != null).length,
    prev: evPrev.items.filter((e) => e.lat != null && ["outbreak", "wild_detection"].includes(e.kind)).length,
    zones: active.length, disinf: disinf.items.length };
  const sw = { birds: "#2a78d6", wx: "#F6D36B", visits: "#7A5AF8", farmpts: "#17201B", farms: "#2F7D4F", cur: "#B42318", prev: "#9A6B6B", zones: "#B54708", disinf: COLOR.disinfection };
  $("#dash-layers").innerHTML = DASH_LAYERS.map((id) => `<label class="lyr"${tipA("lyr." + id)}><input type="checkbox" data-l="${id}" ${dashOn.has(id) ? "checked" : ""}>
    <i style="background:${sw[id]}"></i>${t("lyr." + id, { s: id === "prev" ? prev.label : cur.label })} <span class="muted" ${id === "farmpts" ? 'id="farm-hint"' : id === "visits" ? 'id="visit-hint"' : ""}>${id === "farmpts" ? t("dash.zoomFarms") : id === "wx" || id === "birds" ? "" : fmtNum(counts[id])}</span></label>`).join("");
  const apply = () => DASH_LAYERS.forEach((id) => dashOn.has(id) ? layerData[id]()
    : id === "zones" ? m.circles(id, []) : id === "farms" ? (m.dots(id, []), m.polys("farms-geo", null)) : id === "wx" ? drawWxMap(m, false) : id === "birds" ? drawBirds(m, false) : m.dots(id, []));
  $("#dash-layers").querySelectorAll("input").forEach((cb) => cb.onchange = () => {
    cb.checked ? dashOn.add(cb.dataset.l) : dashOn.delete(cb.dataset.l);
    if (cb.dataset.l === "birds") { S._birdFit = false; if (!cb.checked) m.focus(36.3, 127.8, 7); }
    try { localStorage.setItem("ldt.dash", JSON.stringify([...dashOn])); } catch {}
    apply();
  });
  apply();
  if (!m._farmIdle && m.onIdle) { m._farmIdle = true; m.onIdle(() => dashOn.has("farmpts") && loadFarmPts(m, S.species)); }
  // 농장 레이어 범례: 색 기준 선택 + 5단계 구간
  const fmtK = (v) => v >= 10000 ? `${Math.round(v / 1000).toLocaleString()}k` : fmtNum(Math.round(v));
  $("#dash-legend").innerHTML = dashOn.has("farms") && brk.length
    ? `<span class="muted">${t(hasShapes ? "dash.fillby" : "dash.sizeby")}</span>
       <select id="dash-metric">${["heads", "farms", "avg"].map((k) => `<option value="${k}" ${k === metric ? "selected" : ""}
         ${k !== "farms" && (!dens.has_heads || S.species === "all") ? "disabled" : ""}>${t("dash.m." + k)}</option>`).join("")}</select>` +
      SEQ.map((c, i) => `<span class="sw"><i style="background:${c}"></i>${i === 0 ? "≤" + fmtK(brk[0]) : i === 4 ? ">" + fmtK(brk[3]) : fmtK(brk[i - 1]) + "–" + fmtK(brk[i])}</span>`).join("") +
      (hasShapes ? "" : ` <span class="muted">· ${t("dash.centroid")}</span>`)
    : "";
  const dm = $("#dash-metric"); if (dm) dm.onchange = () => { S.dashMetric = dm.value; viewDash(); };
  $("#dash-note").textContent = !dens.items.length ? t("dash.nofarms")
    : !hasShapes ? t("dash.noshapes")
    : shp.unmatched ? t("dash.unmatched", { n: fmtNum(shp.unmatched) }) : "";
  $("#dash-cur").textContent = t("dash.cur", { s: cur.label, n: counts.cur, z: active.length });

  chart("chart-weekly", { type: "bar", data: { labels: series.items.map((s) => s.week),
    datasets: [{ label: t("kpi.outbreaks"), data: series.items.map((s) => s.outbreak), backgroundColor: "#B42318" },
               { label: t("kpi.wild"), data: series.items.map((s) => s.wild_detection), backgroundColor: "#1D4ED8" }] },
    options: { scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } } });
  showFresh("fr-weekly", series.freshness);
}
const freshMerge = (...ls) => ls.flat().filter(Boolean);

let farmReq = 0;
async function loadFarmPts(m, sp) {
  const v = m.view?.(); if (!v) return;
  const hint = (txt) => { const h = $("#farm-hint"); if (h) h.textContent = txt; };
  if (v.zoom < 10) { m.dots("farmpts", []); hint(t("dash.zoomFarms")); return; }
  const my = ++farmReq;
  const r = await api(`/api/facilities?kind=farm&species=${sp}&bbox=${v.bbox.map((x) => x.toFixed(4)).join(",")}&limit=5000`).catch(() => null);
  if (!r || my !== farmReq) return;
  m.dots("farmpts", r.items.map((f) => ({ ...f, r: 3.5, color: "#17201B", opacity: 0.85, weight: 1, z: 2,
    title: `${f.name || "-"} · ${f.subspecies || f.species || ""} · ${f.status || ""} · ${f.address || ""}` })));
  hint(r.items.length ? t("dash.farmsShown", { n: fmtNum(r.items.length) }) : t("dash.nofarmpts"));
}

/* ── 2. 발생 추적: 시즌 재생 ───────────────────────────────────────────── */
const TL = { data: null, cursor: 0, timer: null, byId: {} };
const dayToDate = (d0, n) => { const x = new Date(d0 + "T00:00:00Z"); x.setUTCDate(x.getUTCDate() + n); return x.toISOString().slice(0, 10); };   // UTC 기준(한국 시각에서 하루 밀리던 문제)
const seasonYearOf = (iso, sp) => { const sm = CFG.species[sp]?.season_start_month || 1; const y = +iso.slice(0, 4), m = +iso.slice(5, 7); return m >= sm ? y : y - 1; };

// 차트 세로선(재생 위치)
const cursorLine = { id: "cursorLine", afterDatasetsDraw(c) {
  if (!TL.data || c.options.plugins.cursorLine === false) return;
  const x = c.scales.x.getPixelForValue(c.config.type === "bar" ? Math.floor(TL.cursor / 7) : TL.cursor);
  const { top, bottom } = c.chartArea; const g = c.ctx;
  g.save(); g.strokeStyle = "#0E5A55"; g.lineWidth = 1.5; g.setLineDash([4, 3]);
  g.beginPath(); g.moveTo(x, top); g.lineTo(x, bottom); g.stroke(); g.restore();
} };
if (window.Chart) Chart.register(cursorLine);

async function viewTrack() {
  if (S.species === "all") { S.species = "poultry"; renderSpeciesTabs(); }
  const sp = S.species;
  const cfg = CFG.species[sp];
  // 질병 선택: 주 질병 기본, 전체 가능
  const dsel = $("#tl-disease");
  const names = S.lang === "en" ? cfg.diseases_en : cfg.diseases;
  if (dsel.dataset.sp !== sp || dsel.dataset.lang !== S.lang) {
    dsel.innerHTML = Object.entries(names).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("") + `<option value="all">${t("tl.alld")}</option>`;
    dsel.value = S.tlDisease && (S.tlDisease in names || S.tlDisease === "all") ? S.tlDisease : cfg.main_disease;
    dsel.dataset.sp = sp; dsel.dataset.lang = S.lang;
  }
  const seas = await api(`/api/seasons?species=${sp}&disease=${dsel.value}`);
  const ssel = $("#tl-season");
  ssel.innerHTML = seas.items.map((x) => `<option value="${x.year}">${x.label} (${fmtNum(x.events)})</option>`).join("") || `<option value="">–</option>`;
  if (S.tlSeason && seas.items.some((x) => x.year === S.tlSeason)) ssel.value = S.tlSeason;
  await loadSeason();
}

async function loadSeason() {
  stopPlay();
  const sp = S.species, year = $("#tl-season").value, dis = $("#tl-disease").value;
  S.tlSeason = +year || null; S.tlDisease = dis;
  const d = await api(`/api/season?species=${sp}&disease=${dis}${year ? "&year=" + year : ""}`);
  TL.data = d; TL.byId = Object.fromEntries((d.events || []).map((e) => [e.id, e]));
  if (!d.events?.length || !d.from) {       // 이 축종·질병의 발생이 없음: 화면을 비우고 안내만
    $("#tl-sum").innerHTML = t("tl.none"); $("#tl-date").textContent = "–"; $("#ev-list").innerHTML = "";
    clearChart("chart-tdist"); clearChart("chart-curve");
    const m0 = S.maps.track; if (m0) { m0.dots("events", []); m0.dots("rings", []); m0.lines?.("links", []); }
    showFresh("fr-track", d.freshness); return;
  }
  const last = d.events?.length ? Math.max(...d.events.map((e) => e.day)) : 0;
  // 진행 중인 시즌은 오늘까지만(미래로 재생하지 않음)
  const endIso = d.to && d.to > new Date().toISOString().slice(0, 10) ? new Date().toISOString().slice(0, 10) : d.to;
  const end = d.from ? Math.max(last, Math.round((Date.parse(endIso) - Date.parse(d.from)) / 864e5)) : 0;
  const sl = $("#tl-slider"); sl.max = end; TL.cursor = Math.min(last, end); sl.value = TL.cursor;
  const s = d.summary || {};
  d.link ||= CFG.species[S.species]?.link || { days: 30, km: 30 };
  $("#tl-sum").innerHTML = d.events?.length ? t("tl.sum", { label: d.label, n: fmtNum(s.events), g: fmtNum(s.with_coords), l: s.linked, j: s.independent,
    c: s.clusters, k: s.largest_cluster, days: d.link.days, km: d.link.km, idx: d.index_case ? `${d.index_case.date} ${esc(d.index_case.place || "")}` : "–" }) : t("tl.none");
  $("#tl-dist-note").textContent = t("tl.distnote", { days: d.link.days, km: d.link.km });
  showFresh("fr-track", d.freshness);
  renderList();
  const m = S.maps.track ||= await makeMap($("#map-track"));
  if (!S.trackIdle) { S.trackIdle = true; m.onIdle(async ({ zoom, bbox }) => {
    if (zoom < 11) return m.dots("farms", []);
    const fs = await api(`/api/facilities?species=${S.species}&bbox=${bbox.join(",")}&limit=3000`);
    m.dots("farms", fs.items.map((f) => ({ ...f, r: f.kind === "farm" ? 3 : 5, square: true, color: COLOR[f.kind] || "#555", opacity: 0.6,
      title: `${f.name || ""} ${f.subspecies || ""} ${f.status || ""}` })));
  }); }
  const pts = (d.events || []).filter((e) => e.lat != null);
  if (pts.length && m.fitPoints) m.fitPoints(pts);
  renderFrame(); drawTimeCharts();
  if (S.pendingOpen && TL.byId[S.pendingOpen]) {
    const e = TL.byId[S.pendingOpen]; TL.cursor = e.day; $("#tl-slider").value = e.day; renderFrame(); openEvent(e.id, false);
  }
  else if (!TL.byId[S.selected]) trackHint();
  S.pendingOpen = null;
}

function trackHint() {
  const ev = (TL.data?.events || []).filter((e) => e.lat != null);
  const last = ev.length ? ev.reduce((a, b) => (b.day > a.day ? b : a)) : null;
  $("#detail").innerHTML = `<div class="pad hint">
    <p>${t("track.howto")}</p>
    ${last ? `<button class="small" id="tl-latest">${t("track.latest")}</button>
      <p class="muted small">${esc(last.date)} · ${kindLabel(last.kind)} · ${esc(last.place || last.sigungu || "")}</p>` : ""}</div>`;
  if (last) $("#tl-latest").onclick = () => openEvent(last.id, false);
}

function visibleEvents() {
  // 재생 시점까지의 발생은 모두 남긴다(누적). '강조' 기간 안의 새 발생만 크게 표시.
  const c = TL.cursor;
  return (TL.data?.events || []).filter((e) => e.day <= c);
}

function renderFrame() {
  const d = TL.data; if (!d || !d.from) return;
  const hw = +$("#tl-window").value || 7, c = TL.cursor;
  const vis = visibleEvents();
  const fresh = vis.filter((e) => c - e.day < hw);
  const ob = vis.filter((e) => e.kind === "outbreak").length;
  $("#tl-date").innerHTML = `${dayToDate(d.from, c)} <span class="muted">· ${t("tl.count", { n: ob, f: fresh.length })}</span>`;
  const m = S.maps.track; if (!m) return;
  const colorOf = (e) => e.kind === "wild_detection" ? COLOR.wild_detection : e.linked ? "#B42318" : "#6D28D9";
  m.dots("events", vis.map((e) => {
    const isNew = c - e.day < hw;
    return { ...e, z: 5, color: colorOf(e),
      r: isNew ? 10 : 7,
      opacity: isNew ? 1 : 0.85, stroke: isNew ? "#17201B" : "#fff", weight: isNew ? 2.5 : 1.5,
      title: `${e.date} ${kindLabel(e.kind)} ${e.disease || ""} ${e.place || ""}${e.prev ? ` · ${t("tl.prevshort", { km: e.prev.km, d: e.prev.days })}` : ""}` };
  }), (e) => openEvent(e.id, false));
  // 방금 나타난 발생은 바깥 고리로 한 번 더 강조
  m.dots("rings", fresh.filter((e) => c - e.day < Math.max(2, +$("#tl-speed").value + 1)).map((e) => ({ ...e,
    r: 18, color: colorOf(e), opacity: 0.08, stroke: colorOf(e), weight: 2, z: 4, interactive: false, title: "" })));
  // 연쇄 가능 연결선은 계속 남긴다(최근 것은 진하게)
  if (m.lines) m.lines("links", vis.filter((e) => e.linked && TL.byId[e.prev.id]).map((e) => {
    const p = TL.byId[e.prev.id]; return { from: [p.lat, p.lon], to: [e.lat, e.lon], color: "#B42318", opacity: c - e.day < hw ? 0.9 : 0.35 };
  }));
  ["chart-tdist", "chart-curve"].forEach((id) => { const ch = S.charts[id]; if (ch?.canvas) ch.draw(); });
}

function drawTimeCharts() {
  const d = TL.data; if (!d || !d.from || !d.events?.length || !window.Chart) return;
  const mode = $("#tl-dmode").value;
  const geo = d.events.filter((e) => e.lat != null && (mode === "index" || e.prev));
  const pt = (e) => ({ x: e.day, y: mode === "index" ? e.dist_index_km : e.prev.km, id: e.id });
  const fmtDay = (v) => dayToDate(d.from, v).slice(5);
  chart("chart-tdist", { type: "scatter", data: { datasets: [
    { label: t("tl.linked"), data: geo.filter((e) => e.linked).map(pt), backgroundColor: "#B42318", pointRadius: 4 },
    { label: t("tl.indep"), data: geo.filter((e) => !e.linked && e.kind === "outbreak").map(pt), backgroundColor: "#6D28D9", pointRadius: 4 },
    { label: t("kind.wild_detection"), data: geo.filter((e) => e.kind === "wild_detection").map(pt), backgroundColor: "#1D4ED8", pointStyle: "triangle", pointRadius: 5 }] },
    options: { parsing: true, scales: {
      x: { type: "linear", min: 0, max: +$("#tl-slider").max, ticks: { callback: fmtDay, maxTicksLimit: 10 } },
      y: { type: mode === "index" ? "linear" : "logarithmic", title: { display: true, text: "km" } } },
      plugins: { tooltip: { callbacks: { label: (it) => { const e = TL.byId[it.raw.id]; return `${e.date} ${e.place || ""} · ${it.raw.y} km`; } } } },
      onClick: (_, els, ch) => { if (!els.length) return; const r = ch.data.datasets[els[0].datasetIndex].data[els[0].index];
        TL.cursor = TL.byId[r.id].day; $("#tl-slider").value = TL.cursor; renderFrame(); openEvent(r.id, false); } } });
  const weeks = Math.ceil((+$("#tl-slider").max + 1) / 7), bars = Array(weeks).fill(0);
  d.events.filter((e) => e.kind === "outbreak").forEach((e) => bars[Math.floor(e.day / 7)]++);
  let acc = 0; const cum = bars.map((b) => (acc += b));
  chart("chart-curve", { type: "bar", data: { labels: bars.map((_, i) => fmtDay(i * 7)), datasets: [
    { type: "bar", label: t("tl.weekly"), data: bars, backgroundColor: "#B42318", yAxisID: "y" },
    { type: "line", label: t("tl.cum"), data: cum, borderColor: "#0E5A55", pointRadius: 0, yAxisID: "y1" }] },
    options: { scales: { y: { beginAtZero: true, ticks: { precision: 0 } }, y1: { position: "right", beginAtZero: true, grid: { drawOnChartArea: false } } },
      onClick: (_, els) => { if (!els.length) return; TL.cursor = Math.min(+$("#tl-slider").max, els[0].index * 7 + 6); $("#tl-slider").value = TL.cursor; renderFrame(); } } });
}

function renderList() {
  const evs = (TL.data?.events || []).slice().reverse();
  $("#tl-listnote").textContent = TL.data?.label ? `${TL.data.label} · ${fmtNum(evs.length)}` : "";
  $("#ev-list").innerHTML = evs.slice(0, 1500).map((e) => `<tr data-id="${e.id}"><td>${e.date}</td><td class="k-${e.kind}">${kindLabel(e.kind)}</td>
    <td>${esc(e.disease || "")}</td><td>${esc(e.subspecies || spLabel(e.species) || "")}</td><td>${esc(e.place || e.sigungu || "")}${e.lat == null ? ' <span class="muted">(–)</span>' : ""}</td>
    <td>${e.prev ? `<span class="${e.linked ? "bad" : "muted"}">${t("tl.prevshort", { km: e.prev.km, d: e.prev.days })}</span>` : `<span class="muted">${e.lat == null ? "" : t("tl.first")}</span>`}</td>
    <td>${e.cluster ? "#" + e.cluster : ""}</td></tr>`).join("") || `<tr><td colspan="7" class="muted">${t("tl.none")}</td></tr>`;
  $("#ev-list").querySelectorAll("tr[data-id]").forEach((tr) => tr.onclick = () => {
    const e = TL.byId[+tr.dataset.id]; TL.cursor = e.day; $("#tl-slider").value = e.day; renderFrame(); openEvent(e.id, false);
    $("#ev-list").querySelectorAll("tr.sel").forEach((x) => x.classList.remove("sel")); tr.classList.add("sel");
    // 지도가 화면 위로 밀려나 있을 때만 지도가 보이게 올린다(목록은 지도 바로 아래라 함께 보임)
    if ($("#map-track").getBoundingClientRect().top < 0) $("#map-track").scrollIntoView({ behavior: "smooth", block: "start" }); });
}

function stopPlay() { clearInterval(TL.timer); TL.timer = null; $("#tl-play").textContent = "▶"; }
$("#tl-play").onclick = () => {
  if (TL.timer) return stopPlay();
  const max = +$("#tl-slider").max;
  // 끝에 있으면 처음부터(첫 발생 직전부터) 다시
  if (TL.cursor >= max) TL.cursor = Math.max(0, Math.min(...(TL.data?.events || []).map((e) => e.day), max) - 3);
  $("#tl-play").textContent = "❚❚";
  const days = (TL.data?.events || []).map((e) => e.day).sort((a, b) => a - b);
  TL.timer = setInterval(() => {
    let next = TL.cursor + +$("#tl-speed").value;
    // 3주 넘게 아무 발생이 없으면 다음 발생 사흘 전으로 건너뛴다(조용한 기간에 기다리지 않도록)
    if ($("#tl-skip").checked) {
      const nd = days.find((x) => x > TL.cursor);
      if (nd != null && nd - TL.cursor > 21) next = Math.max(next, nd - 3);
      if (nd == null) next = max;
    }
    TL.cursor = Math.min(max, next); $("#tl-slider").value = TL.cursor; renderFrame();
    if (TL.cursor >= max) stopPlay();
  }, 220);
};
$("#tl-slider").oninput = (e) => { TL.cursor = +e.target.value; renderFrame(); };
$("#tl-window").onchange = renderFrame;
$("#tl-dmode").onchange = drawTimeCharts;
$("#tl-season").onchange = loadSeason;
$("#tl-disease").onchange = () => viewTrack();

async function openEvent(id, switchView = true) {
  S.selected = id;
  if (switchView && S.view !== "track") {
    const ev = await api(`/api/events/${id}/impact`);
    if (CFG.species[S.species] && ev.event?.date) { S.tlSeason = seasonYearOf(ev.event.date, S.species); S.tlDisease = ev.event.disease; }
    S.pendingOpen = id;
    document.querySelector('#nav button[data-view="track"]').click(); return;
  }
  const d = await api(`/api/events/${id}/impact`);
  const e = d.event;
  const m = S.maps.track;
  let html = `<div class="pad"><span class="tag k-${e.kind}">${kindLabel(e.kind)}</span> <span class="tag">${esc(e.disease || "")}</span>
    <h2 style="font-size:20px;margin:6px 0">${esc(e.place || e.sigungu || "-")}</h2>
    <div class="muted">${[esc([e.sido, e.sigungu].filter(Boolean).join(" ")), t("d.confirmed", { d: e.date }), e.sampled ? t("d.sampled", { d: e.sampled }) : ""].filter(Boolean).join(" · ")}</div>
    ${e.note ? `<p style="font-size:14px;line-height:1.6">${esc(e.note)}</p>` : ""}
    ${e.lat != null && e.source === "mafra_disease_outbreak" ? `<p class="muted" style="font-size:12px">${t("d.approx")}</p>` : ""}</div>`;
  if (d.zones) {
    html += `<div class="pad zones">${Object.entries(d.zones).map(([z, v]) => `<div><span class="muted">${t("d.zone", { km: v.km, z: t(z === "control" ? "d.control" : "d.surv") })}</span>
      ${v.farms || !v.est?.townships ? `<b>${fmtNum(v.farms)}</b><span class="muted">${v.heads ? t("d.heads", { n: fmtNum(v.heads) }) + " · " : ""}${t("d.fac", { n: v.facilities })}</span>`
        : `<b>~${fmtNum(v.est.farms)}</b><span class="muted">${t("d.est", { t: v.est.townships, n: fmtNum(v.est.heads) })}</span>`}</div>`).join("")}</div>`;
    if (d.hubs?.length) html += `<div class="pad"><h2 style="font-size:15px">${t("d.hubs")}</h2><ul class="hubs">${d.hubs.map((h) =>
      `<li><b>${esc(h.name || "")}</b> · ${h.dist_km} km<br><span class="muted">${esc(h.address || "")} · ${esc(h.status || "")}</span></li>`).join("")}</ul></div>`;
    html += `<div class="pad"><h2 style="font-size:15px">${t("d.farms")}</h2>
      <div class="scroll" style="max-height:220px"><table class="tbl"><tbody>${d.farms.slice(0, 50).map((f) =>
      `<tr><td>${esc(f.name || "-")}</td><td>${esc(f.subspecies || f.species || "")}</td><td>${f.dist_km} km</td></tr>`).join("") ||
      `<tr><td class="muted">${t("d.nofarm")}</td></tr>`}</tbody></table></div></div>`;
    if (d.following_events?.length) html += `<div class="pad"><h2 style="font-size:15px">${t("d.after")}</h2>
      <ul>${d.following_events.map((x) => `<li>${x.date} ${kindLabel(x.kind)} ${esc(x.place || "")} (${x.dist_km} km)</li>`).join("")}</ul></div>`;
    m?.dots("hubs", (d.hubs || []).map((h) => ({ ...h, r: 6, color: COLOR.disinfection, square: true, z: 5, title: `${h.name} · ${h.dist_km} km` })));
    m?.circles("zone", [{ lat: e.lat, lon: e.lon, km: d.radii_km.surveillance, color: "#B54708", dashed: true },
                        { lat: e.lat, lon: e.lon, km: d.radii_km.control, color: "#B42318", fill: 0.1 }]);
    m?.focus(e.lat, e.lon, 11);
  } else {
    html += `<p class="pad muted">${t("d.nocoord")}</p>`;
    m?.circles("zone", []);
  }
  html += `<div class="pad" id="visit-box"><span class="muted">${t("visit.loading")}</span></div>`;
  html += `<div class="fresh">${freshHTML(d.freshness)}</div>`;
  $("#detail").innerHTML = html;
  loadVisits(id, d.radii_km?.control || 3);
}

// 발생 전 21일: 발생 리와 관리지역(3km) 안 리에 다녀간 축산차량 (KAHIS 공개 방문정보, 시설명·차량번호 비공개)
async function loadVisits(id, km) {
  const box = $("#visit-box"); if (!box) return;
  const v = await api(`/api/visits/near?event_id=${id}&km=${km}&days=21`).catch(() => null);
  if (S.selected !== id || !$("#visit-box")) return;
  if (!v || !v.days_covered) { box.innerHTML = `<h2 style="font-size:15px">${t("visit.title", { km })}</h2><p class="muted">${t("visit.nodata")}</p>`; return; }
  const max = Math.max(1, ...v.by_vehicle.map((x) => x[1]));
  const bars = (list) => list.slice(0, 8).map(([k, n]) => `<div class="vbar"><span>${esc(k)}</span><i style="width:${Math.max(2, 100 * n / max)}%"></i><b>${fmtNum(n)}</b></div>`).join("");
  box.innerHTML = `<h2 style="font-size:15px">${t("visit.title", { km })}</h2>
    <p class="muted" style="margin:2px 0 8px">${t("visit.sum", { a: v.from, b: v.to, n: fmtNum(v.total), s: fmtNum(v.same_place), c: v.days_covered, d: v.days })}</p>
    ${v.total ? `<div class="vbars">${bars(v.by_vehicle)}</div>
    <p class="muted small" style="margin-top:8px">${t("visit.fac")}: ${v.by_facility.slice(0, 5).map(([k, n]) => `${esc(k)} ${fmtNum(n)}`).join(" · ")}</p>
    <details><summary class="muted small">${t("visit.places")}</summary><ul class="hubs">${v.places.map((p) => `<li>${esc(p.address)} · ${fmtNum(p.n)}${p.km != null ? ` · ${p.km} km` : ""}</li>`).join("")}</ul></details>` : ""}`;
}

/* ── 3. 연관 분석 ─────────────────────────────────────────────────────── */
async function viewCorr() {
  const sp = S.species === "all" ? "poultry" : S.species;
  loadAssoc(sp, S.tlDisease || CFG.species[sp].main_disease);
  if (!S.tlSeason) { const se = await api(`/api/seasons?species=${sp}&disease=${S.tlDisease || CFG.species[sp].main_disease}`); S.tlSeason = se.items[0]?.year || null; }
  const sq = `${S.tlSeason ? "&season=" + S.tlSeason : ""}&disease=${S.tlDisease || CFG.species[sp].main_disease}`;
  const [series, knox, risk, wx] = await Promise.all([api(`/api/series?species=${sp}${sq}`),
    api(`/api/spacetime?species=${sp}${sq}`), api(`/api/risk?species=${sp}`), api(`/api/weather`)]);
  chart("chart-corr", { type: "bar", data: { labels: series.items.map((s) => s.week), datasets: [
    { label: t("kpi.wild"), data: series.items.map((s) => s.wild_detection), backgroundColor: "#1D4ED8" },
    { label: t("kpi.outbreaks"), data: series.items.map((s) => s.outbreak), backgroundColor: "#B42318" }] },
    options: { scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } } });
  showFresh("fr-corr", series.freshness);

  $("#knox").innerHTML = `<p style="font-size:15px">${t("knox.line", { o: knox.observed, e: knox.expected })}${knox.knox_ratio ? t("knox.ratio", { r: knox.knox_ratio }) : ""}</p>
    <p class="muted">${t("knox.clusters", { n: knox.clusters.length })}${knox.clusters.length ? ": " + knox.clusters.map((c) => c.length).join(", ") : ""}</p>`;
  showFresh("fr-knox", knox.freshness);

  const stns = [...new Set(wx.items.map((w) => w.place))];
  const sel = $("#stn");
  const prev = sel.value;
  sel.innerHTML = stns.map((s) => `<option>${esc(s)}</option>`).join("") || `<option>${t("wx.none")}</option>`;
  if (stns.includes(prev)) sel.value = prev;
  sel.onchange = () => drawWx(wx.items);
  drawWx(wx.items);
  showFresh("fr-wx", wx.freshness);

  const top = risk.regions.slice(0, 10);
  const pal = ["#0E5A55", "#5FA8A0", "#1D4ED8", "#B42318", "#B54708", "#7A5AF8"];
  chart("chart-risk", { type: "bar", data: { labels: top.map((r) => r.region),
    datasets: FACTORS.map((f, i) => ({ label: t("factor." + f), data: top.map((r) => r.contrib[f]), backgroundColor: pal[i] })) },
    options: { indexAxis: "y", scales: { x: { stacked: true }, y: { stacked: true } } } });
  showFresh("fr-riskbars", risk.freshness);

  // 축산차량 방문: 상위 5개 차량유형 + 기타 (축종 탭 기준)
  const vs = await api(`/api/visits/series?species=${S.species}&days=60`).catch(() => ({ items: [] }));
  const vdates = [...new Set(vs.items.map((r) => r.date))].sort();
  const vtot = {}; vs.items.forEach((r) => (vtot[r.vehicle] = (vtot[r.vehicle] || 0) + r.n));
  const vtop = Object.entries(vtot).sort((a, b) => b[1] - a[1]).slice(0, 5).map(([v]) => v);
  const VPAL = ["#0E5A55", "#1D4ED8", "#B54708", "#7A5AF8", "#B42318", "#8A8F8C"];
  const vser = [...vtop, t("visit.other")].map((v, i) => ({ label: v, borderColor: VPAL[i], backgroundColor: VPAL[i], pointRadius: 0, borderWidth: 2,
    data: vdates.map((d) => vs.items.filter((r) => r.date === d && (i < vtop.length ? r.vehicle === v : !vtop.includes(r.vehicle))).reduce((a, r) => a + r.n, 0)) }));
  chart("chart-visits", { type: "line", data: { labels: vdates.map((d) => d.slice(5)), datasets: vser },
    options: { plugins: { cursorLine: false, tooltip: { mode: "index", intersect: false } }, interaction: { mode: "index", intersect: false },
      scales: { y: { beginAtZero: true } } } });
  $("#visit-cov").textContent = vdates.length ? t("visit.cov", { a: vdates[0], b: vdates[vdates.length - 1], n: vdates.length }) : t("visit.none");
}


/* ── 연관 분석: 요인별 '위험 배수'(다른 조건이 같을 때 발생 위험이 몇 배인지) ───────── */
const xN = (v) => v == null || !isFinite(v) ? "–" : v >= 1000 ? ">1000" : v >= 100 ? Math.round(v).toLocaleString() : v >= 10 ? v.toFixed(1) : v < 0.001 ? "<0.001" : v.toFixed(2);
const rngTxt = (lo, hi) => lo == null ? "" : `${xN(lo)}~${xN(hi)}`;
const QL = (q) => t("as.ql" + q);                  // 하위 20% … 상위 20%

// 모형 요인을 실제 단위로 읽히게: 로그 요인은 '몇 배 많을 때', 기온은 '몇 ℃ 낮을 때'(배수 뒤집기)
function asFactor(c) {
  const k = (v) => Math.exp(v).toFixed(1);
  const o = { name: c.factor, v: c.or, lo: c.lo, hi: c.hi, sig: c.sig };
  switch (c.factor) {
    case "density": o.label = t("asl.density", { k: k(c.sd) }); break;
    case "heads_per_farm": o.label = t("asl.heads_per_farm", { k: k(c.sd) }); break;
    case "visits": o.label = t("asl.visits", { k: k(c.sd) }); break;
    case "duck_share": o.label = t("asl.duck_share", { p: Math.max(1, Math.round(c.sd * 100)) }); break;
    case "temperature":
      o.label = t("asl.temperature", { d: (+c.sd).toFixed(1) });
      o.v = c.or ? 1 / c.or : null; o.lo = c.hi ? 1 / c.hi : null; o.hi = c.lo ? 1 / c.lo : null; break;
    default: o.label = t("asl." + c.factor);
  }
  ["v", "lo", "hi"].forEach((f) => { if (o[f] != null) o[f] = Math.round(o[f] * 100) / 100; });
  return o;
}

// 숲 그림(SVG): 점 = 추정 위험 배수, 막대 = 95% 오차 범위, 점선 = 1배(차이 없음)
function forest(rows) {
  const ok = rows.filter((r) => r.v != null && isFinite(r.v));
  if (!ok.length) return `<p class="muted small">${t("as.nodata")}</p>`;
  const lo = Math.max(0.05, Math.min(0.5, ...ok.map((r) => Math.max(0.05, r.lo ?? r.v))));
  const hi = Math.min(100, Math.max(2, ...ok.map((r) => Math.min(100, r.hi ?? r.v))));
  const L = 250, R = 540, W = 690, H = rows.length * 26 + 28;
  const X = (v) => L + (Math.log(Math.min(hi, Math.max(lo, v))) - Math.log(lo)) / (Math.log(hi) - Math.log(lo)) * (R - L);
  const ticks = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50].filter((x) => x >= lo && x <= hi);
  const col = (r) => r.ref ? "var(--muted)" : r.sig ? (r.v > 1 ? "#B42318" : "#1D4ED8") : "#8A8F8C";
  const body = rows.map((r, i) => {
    const y = 18 + i * 26;
    const lab = `<text x="${L - 8}" y="${y + 4}" text-anchor="end">${r.label}</text>`;
    if (r.v == null || !isFinite(r.v)) return lab + `<text x="${L}" y="${y + 4}" class="m">${r.note || t("as.few")}</text>`;
    const ci = r.lo != null && !r.ref ? `<line x1="${X(r.lo)}" x2="${X(r.hi)}" y1="${y}" y2="${y}" stroke="${col(r)}" stroke-width="2.5"/>` : "";
    const val = r.ref ? t("as.ref") : `×${xN(r.v)}`;
    return lab + ci + `<circle cx="${X(r.v)}" cy="${y}" r="${r.ref ? 4 : 5.5}" fill="${col(r)}"><title>${esc(r.tip || "")}</title></circle>` +
      `<text x="${R + 10}" y="${y + 4}" ${r.sig ? 'font-weight="700"' : 'class="m"'}>${val}${r.lo != null && !r.ref ? ` <tspan class="m" font-weight="400">(${rngTxt(r.lo, r.hi)})</tspan>` : ""}</text>`;
  }).join("");
  const axis = ticks.map((x) => `<line x1="${X(x)}" x2="${X(x)}" y1="6" y2="${H - 16}" stroke="var(--soft)"/><text x="${X(x)}" y="${H - 2}" text-anchor="middle" class="m">×${x}</text>`).join("");
  return `<svg class="forest" viewBox="0 0 ${W} ${H}" role="img">${axis}<line class="ref" x1="${X(1)}" x2="${X(1)}" y1="4" y2="${H - 16}"/>${body}</svg>`;
}
const legendLine = () => `<p class="muted small as-how">${t("as.how")}</p>`;
const verdict = (txt) => `<p class="as-verdict">${txt}</p>`;

async function loadAssoc(sp, dis) {
  clearTimeout(S.assocPoll);
  if (S.species === "all") { $("#as-status").textContent = t("as.pickspecies"); return; }
  const d = await api(`/api/assoc?species=${sp}&disease=${dis}`).catch((e) => ({ status: "error", error: String(e) }));
  if (S.view !== "corr") return;
  if (d.status === "computing" || d.status === "updating") S.assocPoll = setTimeout(() => S.view === "corr" && loadAssoc(sp, dis), 4000);
  if (d.status === "computing") {
    $("#as-status").innerHTML = `<span class="spin"></span>${t("as.computing")}`;
    ["#as-forest", "#as-wild", "#as-visits", "#as-density", "#as-birds", "#as-temp"].forEach((id) => { $(id).innerHTML = `<p class="muted">${t("as.wait")}</p>`; });
    return;
  }
  if (d.status === "na" || d.error) { $("#as-status").textContent = d.error ? t("as.error", { e: d.error }) : t("as.pickspecies"); return; }
  if (d.empty) { $("#as-status").textContent = t("as.empty"); return; }
  $("#as-status").innerHTML = (d.status === "updating" ? `<span class="spin"></span>${t("as.updating")} · ` : "") +
    t("as.basis", { r: fmtNum(d.regions), u: fmtNum(d.units), c: fmtNum(d.cases), s: d.seasons,
      m: d.months.map((m) => S.lang === "en" ? m : m + "월").join(", "), dis: dis, sec: d.sec });
  drawAssocModel(d.model || {});
  drawAssocWild(d.wild || {});
  drawAssocTemp(d.temp || {});
  drawAssocVisits(d.visits || {});
  drawAssocDensity(d.density || {});
  drawAssocBirds(d.birds || {});
}

const asReason = (x) => {
  let s = t("as.r." + (x.reason || "error"), { e: x.error || "", n: x.cases ?? x.wild_events ?? "" });
  const b = x.backfill;
  if (b && (b.running || b.total)) s += " " + t(b.running ? "as.wx.running" : "as.wx.done", { d: b.done, t: b.total, r: fmtNum(b.rows || 0) });
  return s;
};

async function drawAssocModel(M) {
  if (!M.available) { $("#as-forest").innerHTML = `<p class="muted">${asReason(M)}</p>`; $("#as-models").innerHTML = ""; $("#as-pred").innerHTML = ""; $("#as-model-sum").textContent = ""; return; }
  const best = M.models[M.best];
  const fs = best.coef.map(asFactor);
  const up = fs.filter((f) => f.sig && f.v > 1).sort((a, b) => b.v - a.v);
  const down = fs.filter((f) => f.sig && f.v < 1);
  const none = fs.filter((f) => !f.sig);
  const strip = (s) => s.replace(/<[^>]+>/g, "");
  $("#as-forest").innerHTML = verdict(
      (up.length ? t("as.v.up", { l: up.map((f) => `<b>${strip(f.label)}</b> ×${xN(f.v)}`).join(", ") }) : t("as.v.noup")) +
      (down.length ? " " + t("as.v.down", { l: down.map((f) => `${strip(f.label)} ×${xN(f.v)}`).join(", ") }) : "") +
      (none.length ? `<br><span class="muted">${t("as.v.none", { l: none.map((f) => strip(f.label)).join(", ") })}</span>` : "")) +
    forest(fs.map((f) => ({ ...f, tip: `×${xN(f.v)} (${rngTxt(f.lo, f.hi)})` }))) + legendLine();
  const ev = best.oot || { auc: best.auc };
  $("#as-model-sum").textContent = t("as.modelsum", { a: ev.auc ?? "–", s: best.oot ? best.oot.season : t("as.insample") });
  $("#as-models").innerHTML = `<thead><tr><th>${t("as.m.model")}</th><th>${t("as.m.period")}</th><th>${t("as.m.units")}</th><th>${t("as.m.cases")}</th>
    <th data-tip="tip.as.auc">${t("as.m.auc")}</th><th data-tip="tip.as.oot">${t("as.m.oot")}</th></tr></thead><tbody>` +
    M.models.map((m, i) => `<tr${i === M.best ? ' class="on"' : ""}><td>${i === 0 ? t("as.m.base") : "+ " + m.features.slice(M.models[i - 1].features.length).map((f) => t("asf." + f)).join(", ")}</td>
      <td class="nowrap">${m.from.slice(0, 7)} ~ ${m.to.slice(0, 7)}</td><td>${fmtNum(m.units)}</td><td>${fmtNum(m.cases)}</td>
      <td>${m.auc ?? "–"}</td><td>${m.oot ? `${m.oot.auc} <span class="muted">${m.oot.season}</span>` : "–"}</td></tr>`).join("") + "</tbody>";
  const P = M.pred || [];
  const pct = (p) => p >= 0.01 ? (p * 100).toFixed(1) + "%" : p >= 0.0001 ? (p * 100).toFixed(2) + "%" : "<0.01%";
  $("#as-pred-h").textContent = P.length ? t("as.predh", { w: M.week }) : t("as.nopred");
  const fac = (f) => f ? t("asf." + f) : "–";
  $("#as-pred").innerHTML = P.length ? `<thead><tr><th>${t("th.rank")}</th><th>${t("th.region")}</th><th data-tip="tip.as.p">${t("as.p")}</th>
    <th data-tip="tip.as.rel">${t("as.rel")}</th><th>${t("as.topf")}</th></tr></thead><tbody>` +
    P.slice(0, 10).map((r, i) => `<tr><td>${i + 1}</td><td><b>${esc(r.region)}</b></td><td>${pct(r.p)}</td><td>×${xN(r.rel)}</td><td>${fac(r.top)}</td></tr>`).join("") + "</tbody>" : "";
  const m = S.maps.assoc ||= await makeMap($("#map-assoc"));
  const rels = P.map((r) => r.rel).sort((a, b) => a - b);
  const q = (p) => rels[Math.min(rels.length - 1, Math.floor(rels.length * p))] || 1;
  const cuts = [q(0.5), q(0.75), q(0.9)];
  const RAMP = ["#F2D98C", "#EDA100", "#E0611A", "#B42318"];
  const ci = (v) => cuts.filter((c) => v > c).length;
  m.dots("pred", P.map((r) => ({ lat: r.lat, lon: r.lon, r: 4 + 9 * Math.sqrt(Math.min(r.rel, 6) / 6), color: RAMP[ci(r.rel)], opacity: 0.85, z: 3,
    title: `${r.region} · ${t("as.p")} ${pct(r.p)} · ${t("as.rel")} ×${xN(r.rel)} · ${fac(r.top)}` })));
  $("#as-pred-legend").innerHTML = P.length ? `<span class="muted small">${t("as.rel")}</span>` + RAMP.map((c, i) =>
    `<span class="chip"><i style="background:${c}"></i>${i === 0 ? "≤×" + xN(cuts[0]) : i === 3 ? ">×" + xN(cuts[2]) : `×${xN(cuts[i - 1])}–${xN(cuts[i])}`}</span>`).join("") : "";
}

function drawAssocWild(W) {
  if (!W.available) { $("#as-wild").innerHTML = `<p class="muted">${asReason(W)}</p>` + (W.reason === "few_wild" || W.reason === "no_wild" ? `<p class="small as-note">${t("as.wild.get")}</p>` : ""); return; }
  const max = Math.max(2, ...W.grid.flatMap((g) => g.cells.map((c) => c.rr || 0)));
  const cell = (c) => {
    if (c.rr == null) return `<td class="muted" title="${t("as.few")}">–</td>`;
    const a = c.rr > 1 ? Math.min(1, Math.log(c.rr) / Math.log(max)) : 0;
    return `<td class="${c.sig ? "sig" : ""}" style="background:${c.rr > 1 ? `rgba(180,35,24,${(0.1 + 0.8 * a).toFixed(2)})` : "var(--soft)"};color:${a > 0.5 ? "#fff" : "inherit"}"
      title="${t("as.cellTip", { rr: xN(c.rr), lo: xN(c.lo), hi: xN(c.hi), a: c.a, n1: fmtNum(c.n1), r1: c.rate1, r0: c.rate0 })}">×${xN(c.rr)}</td>`;
  };
  const best = W.grid.flatMap((g) => g.cells.map((c) => ({ ...c, km: g.km }))).filter((c) => c.sig && c.rr > 1).sort((a, b) => b.lo - a.lo)[0];   // 가장 확실한(하한이 큰) 칸
  $("#as-wild").innerHTML = verdict(best ? t("as.wild.v", { km: best.km, lag: best.lag, rr: xN(best.rr), lo: xN(best.lo), hi: xN(best.hi) }) : t("as.wild.v0")) +
    `<table class="heat as-grid"><thead><tr><th></th>${W.grid[0].cells.map((c) => `<th>${t("as.lagd", { d: c.lag })}</th>`).join("")}</tr></thead>
    <tbody>${W.grid.map((g) => `<tr><th>${t("as.within", { km: g.km })}</th>${g.cells.map(cell).join("")}</tr>`).join("")}</tbody></table>
    <p class="as-note muted small">${t("as.wildnote", { n: fmtNum(W.wild_events), u: fmtNum(W.units), c: fmtNum(W.cases) })}</p>`;
}

function drawAssocTemp(T) {
  if (!T.available) { clearChart("chart-as-temp"); $("#as-temp").innerHTML = asReason(T); return; }
  const lab = (b) => b.tmin <= -99 ? `${b.tmax}℃ ${t("as.below")}` : b.tmax >= 99 ? `${b.tmin}℃ ${t("as.above")}` : `${b.tmin}~${b.tmax}℃`;
  const bs = T.bins.filter((b) => b.units);
  chart("chart-as-temp", { type: "bar", data: { labels: bs.map(lab), datasets: [{ label: t("as.rate"), data: bs.map((b) => b.rate1 ?? 0),
    backgroundColor: bs.map((b) => b.ref ? "#8A8F8C" : b.sig && b.rr > 1 ? "#B42318" : "#E8A39B") }] },
    options: { plugins: { legend: { display: false }, tooltip: { callbacks: { afterLabel: (c) => { const b = bs[c.dataIndex];
      return b.ref ? t("as.refband") : b.rr != null ? `${t("as.vsref")} ×${xN(b.rr)} (${rngTxt(b.lo, b.hi)}) · ${t("as.units", { u: fmtNum(b.units), c: b.cases })}` : t("as.few"); } } } },
      scales: { y: { beginAtZero: true, title: { display: true, text: t("as.rate") } }, x: { title: { display: true, text: t("as.tempx") } } } } });
  const ref = bs.find((b) => b.ref), top = bs.filter((b) => b.sig && b.rr > 1).sort((a, b) => b.rr - a.rr)[0];
  const c = T.coverage;
  $("#as-temp").innerHTML = (top && ref ? verdict(t("as.temp.v", { a: lab(top), b: lab(ref), rr: xN(top.rr), lo: xN(top.lo), hi: xN(top.hi) })) : "") +
    t("as.tempnote", { a: c?.from || "–", b: c?.to || "–", c: c?.cases ?? "–" });
}

function drawAssocVisits(V) {
  if (!V.available) { $("#as-visits").innerHTML = `<p class="small as-note">${t("as.li")}</p><p class="muted">${asReason(V)}</p>`; return; }
  const VB = (q) => t("as.vb" + q);
  const rng = (d) => d.q === 0 ? t("as.v0") : d.vmax == null ? t("as.vmore", { a: fmtNum(d.vmin) }) : t("as.vrange", { a: fmtNum(d.vmin), b: fmtNum(d.vmax) });
  const ref = V.dose.filter((d) => d.ref), top = V.dose[V.dose.length - 1];
  const refTxt = ref.length > 1 ? t("as.vrange", { a: 0, b: fmtNum(ref[ref.length - 1].vmax) }) : t("as.v0");
  const vth = (x) => x.threshold > 0 ? t("as.vth", { n: fmtNum(x.threshold) }) : t("as.vth1");
  const risk = V.by_vehicle.filter((x) => !x.response), resp = V.by_vehicle.filter((x) => x.response);
  const vk = risk.filter((x) => x.sig && x.or > 1);
  let html = `<p class="small as-note">${t("as.li")}</p>` +
    (top.or && !top.ref ? verdict(t("as.visit.v", { a: fmtNum(top.vmin), b: refTxt, rr: xN(top.or), lo: xN(top.lo), hi: xN(top.hi) }) +
      (vk.length ? " " + t("as.visit.v2", { l: vk.slice(0, 3).map((x) => `${esc(x.vehicle)} ×${xN(x.or)}`).join(", ") }) : "")) : "") +
    `<b>${t("as.dose")}</b>` +
    forest(V.dose.map((d) => ({ label: `${rng(d)} <tspan class="m">${VB(d.q)}</tspan>`, v: d.or, lo: d.ref ? null : d.lo,
      hi: d.ref ? null : d.hi, sig: d.sig, ref: d.ref, tip: t("as.cc", { a: d.cases, b: fmtNum(d.ctrls) }) }))) +
    `<b>${t("as.byveh")}</b>` + forest(risk.map((x) => ({ label: `${esc(x.vehicle)} <tspan class="m">${vth(x)}</tspan>`,
      v: x.or, lo: x.lo, hi: x.hi, sig: x.sig }))) +
    (resp.length ? `<b>${t("as.resp")}</b>` + forest(resp.map((x) => ({ label: `${esc(x.vehicle)} <tspan class="m">${vth(x)}</tspan>`,
      v: x.or, lo: x.lo, hi: x.hi, sig: false }))) : "") + legendLine();
  const N = V.now;
  if (N && N.rows.length) html += `<b>${t("as.now", { a: N.from, b: N.to })}</b><div class="scroll"><table class="tbl small"><thead><tr><th>${t("as.now.li")}</th>
      <th>${t("as.now.n")}</th><th>${t("as.now.grp")}</th><th>${t("as.now.veh")}</th></tr></thead><tbody>` +
    N.rows.map((r) => `<tr><td>${esc(r.address)}</td><td>${fmtNum(r.visits)}</td><td class="${r.group >= V.nbins - 1 ? "bad" : ""}">${VB(r.group)}</td><td>${r.top.map(esc).join(", ")}</td></tr>`).join("") + "</tbody></table></div>";
  html += `<p class="as-note muted small">${t("as.visitnote", { a: V.coverage.from, b: V.coverage.to, w: V.coverage.weeks, c: V.cases, k: fmtNum(V.ctrls) })}</p>`;
  $("#as-visits").innerHTML = html;
}

function drawAssocDensity(D) {
  if (!D.available) { $("#as-density").innerHTML = `<p class="muted">${asReason(D)}</p>`; return; }
  const fmtv = (f, v) => v == null ? "–" : f === "duck_share" ? `${Math.round(v * 100)}%` : fmtNum(Math.round(v));
  const unit = (f) => t("asu." + f);
  $("#as-density").innerHTML = D.factors.map((F) => {
    const top = F.rows[F.top_q - 1], r1 = F.rows[0];
    const range = (r) => `${fmtv(F.factor, r.vmin)}~${fmtv(F.factor, r.vmax)}${unit(F.factor)}`;
    const v = top.sig && top.rr > 1
      ? t("as.dens.v", { f: t("asd." + F.factor), a: range(top), q: QL(F.top_q), b: range(r1), rr: xN(top.rr), lo: xN(top.lo), hi: xN(top.hi) })
      : t("as.dens.v0", { f: t("asd." + F.factor) });
    return `<b>${t("asd." + F.factor)}</b>` + verdict(v) + forest(F.rows.map((r) => ({
      label: `${range(r)} <tspan class="m">${QL(r.q)}</tspan>`, v: r.rr, lo: r.q === 1 ? null : r.lo, hi: r.q === 1 ? null : r.hi, sig: r.sig, ref: r.q === 1,
      tip: `${t("as.regions", { n: r.regions })} · ${t("as.units", { u: fmtNum(r.units), c: r.cases })}` }))) +
      (top.sig && top.rr > 1 ? `<p class="small as-note">${t("as.dens.list", { q: QL(F.top_q), n: F.top_n })} ${F.top_regions.map(esc).join(", ")}${F.top_n > F.top_regions.length ? " …" : ""}</p>` : "");
  }).join("") + legendLine() + `<p class="as-note muted small">${t("as.densnote")}</p>`;
}

function drawAssocBirds(B) {
  if (!B.available) { clearChart("chart-as-birds"); $("#as-birds").innerHTML = `<p class="muted">${asReason(B)}</p>`; return; }
  const wk = B.weeks.map((w) => { const d = new Date(Date.UTC(2026, 0, 1 + w * 7)); return `${d.getUTCMonth() + 1}/${d.getUTCDate()}`; });
  chart("chart-as-birds", { type: "bar", data: { labels: wk, datasets: [
    { type: "bar", label: t("as.b.ob"), data: B.outbreaks, backgroundColor: "#B42318", yAxisID: "y" },
    { type: "line", label: t("as.b.dep"), data: B.departs, borderColor: "#1D4ED8", backgroundColor: "#1D4ED8", pointRadius: 2, yAxisID: "y1" }] },
    options: { scales: { y: { beginAtZero: true, title: { display: true, text: t("as.b.ob") } },
      y1: { beginAtZero: true, position: "right", grid: { drawOnChartArea: false }, title: { display: true, text: t("as.b.dep") } } } } });
  $("#as-birds").innerHTML = (B.lag_median != null ? verdict(t("as.b.sum", { m: B.lag_median, a: B.lag_range[0], b: B.lag_range[1] })) : "") +
    `<table class="tbl small"><thead><tr><th>${t("as.b.season")}</th><th>${t("as.b.tracks")}</th><th>${t("as.b.med")}</th>
    <th>${t("as.b.p90")}</th><th data-tip="tip.as.lag">${t("as.b.lag")}</th><th>${t("as.b.after")}</th></tr></thead><tbody>` +
    B.rows.map((r) => `<tr><td>${r.season}</td><td>${r.tracks}</td><td>${r.depart_median.slice(5)}</td><td>${r.p90 ? r.p90.slice(5) : "–"}</td>
      <td>${r.lag_p90 ?? "–"}${r.lag_p90 != null ? t("as.days") : ""}</td><td>${r.after_median}/${r.outbreaks}</td></tr>`).join("") + "</tbody></table>" +
    `<p class="as-note muted small">${t("as.b.note")}</p>`;
}

function drawWx(rowsAll) {
  const rows = rowsAll.filter((w) => w.place === $("#stn").value);
  const dates = [...new Set(rows.map((r) => r.date))];
  const get = (m) => dates.map((d) => rows.find((r) => r.date === d && r.metric === m)?.value ?? null);
  chart("chart-wx", { type: "line", data: { labels: dates, datasets: [
    { label: t("wx.temp"), data: get("temp_avg"), borderColor: "#B54708", yAxisID: "y", pointRadius: 0 },
    { label: t("wx.hum"), data: get("humidity_avg"), borderColor: "#1D4ED8", yAxisID: "y1", pointRadius: 0 }] },
    options: { scales: { y: { position: "left" }, y1: { position: "right", grid: { drawOnChartArea: false } } } } });
}

/* ── 4. 데이터 관리 ───────────────────────────────────────────────────── */
function renderAuto(a) {
  $("#auto-state").innerHTML = !a.enabled ? t("auto.off")
    : a.running ? `<b>${t("auto.running", { k: t("auto." + a.running), t: fmtTime(a.started_at) })}</b>`
    : t("auto.next", { at: a.at, n: a.next ? fmtTime(a.next) : "–" });
  $("#auto-run").disabled = !!a.running;
  const v = a.visits;
  const vpct = v ? Math.round(100 * v.days_done / Math.max(1, v.days_total)) : 0;
  $("#auto-hist").innerHTML = (v ? `<div class="visit-prog"><b>${t("visit.bf", { p: vpct, n: fmtNum(v.days_done), t: fmtNum(v.days_total), v: fmtNum(v.visits) })}</b>
      ${v.from ? ` · ${v.from} ~ ${v.to}` : ""}${v.running ? ` · ${t("visit.bfnow", { d: v.current, l: fmtNum(v.left) })}${v.last_sec ? ` · ${t("visit.bflast", { s: v.last_sec, n: fmtNum(v.last_visits) })}` : ""}` : ""}
      ${v.error ? `<br><span class="bad">${esc(v.error)}</span>` : ""}<div class="bar"><i style="width:${vpct}%"></i></div></div>` : "") +
    `<p style="margin:0 0 6px">${t("auto.hint")}</p>` + (a.history || []).map((h) =>
    `<div><span class="${h.ok ? "ok" : "bad"}">●</span> ${fmtTime(h.finished_at)} · ${t("auto." + h.source.split(":")[1])} · ${esc(h.message || "")}</div>`).join("");
}

async function viewData() {
  const [s, a] = await Promise.all([api("/api/sources"), api("/api/auto")]);
  renderAuto(a);
  if (a.running || a.visits?.running) { clearTimeout(S.autoPoll); S.autoPoll = setTimeout(() => S.view === "data" && viewData(), 15000); }
  $("#tbl-counts").textContent = `${s.dialect} · ` + Object.entries(s.tables).map(([k, v]) => `${k} ${fmtNum(v)}`).join(" · ");
  $("#src-list").innerHTML = s.sources.map((x) => {
    const f = x.freshness || {};
    const period = f.data_to ? (f.data_from && f.data_from !== f.data_to ? `${f.data_from} ~ ${f.data_to}` : f.data_to) : "–";
    const last = x.last;
    const result = last ? (last.ok ? `<span class="ok">${t("data.rows", { n: fmtNum(last.rows) })}</span>` : `<span class="bad">${esc(last.message)}</span>`) : "";
    return `<tr><td><b>${esc(S.lang === "en" ? x.title_en : x.title)}</b><br><span class="muted">${x.id}</span></td>
      <td>${x.target}</td><td>${t("refresh." + (x.refresh || "manual"))}</td><td>${period}</td>
      <td>${f.collected_at ? fmtTime(f.collected_at) : "–"}${f.stale ? ` <b class="stale">${t("fr.stale")}</b>` : ""}</td>
      <td>${result}</td>
      <td class="nowrap">${f.rows ? `<button class="small" data-view="${x.id}">${t("data.view")}</button> ` : ""}${x.configured ? `<button class="small" data-run="${x.id}">${t("data.run")}</button>` : `<span class="muted">${t("data.unset")}</span>`}</td></tr>`;
  }).join("");
  // '보기'는 데이터 조회 화면으로 이동
  $("#src-list").querySelectorAll("[data-view]").forEach((b) => b.onclick = () => {
    XP.sid = b.dataset.view; XP.tab = "table"; Object.assign(XP, { offset: 0, filters: {}, q: "", from: "", to: "", sort: null });
    xpSave(); document.querySelector('#nav button[data-view="explore"]').click(); });
  $("#src-list").querySelectorAll("[data-run]").forEach((b) => b.onclick = async () => {
    b.disabled = true; b.textContent = t("data.running");
    try { await api(`/api/collect/${b.dataset.run}`, { method: "POST" }); } catch (e) { alert(e.message); }
    viewData(); lastCollect();
  });
}

$("#auto-run").onclick = async () => {
  $("#auto-run").disabled = true;
  await api("/api/auto/run", { method: "POST" });
  setTimeout(() => { viewData(); lastCollect(); }, 800);
};

/* 원천별 데이터 표 */
const PAGE = 100;
async function browse() {
  const B = S.browse; if (!B) return;
  const r = await api(`/api/data/${B.sid}?limit=${PAGE}&offset=${B.offset}${B.q ? "&q=" + encodeURIComponent(B.q) : ""}`);
  $("#browse").hidden = false;
  $("#browse-title").textContent = `${S.lang === "en" ? r.title_en : r.title} · ${r.table}`;
  $("#browse-count").textContent = r.total ? t("data.count", { a: B.offset + 1, b: Math.min(B.offset + PAGE, r.total), n: fmtNum(r.total) }) : "";
  const cols = r.items.length ? Object.keys(r.items[0]) : [];
  $("#browse-tbl").innerHTML = r.items.length
    ? `<thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>${r.items.map((it) =>
      `<tr>${cols.map((c) => `<td>${esc(c === "collected_at" ? fmtTime(it[c]) : typeof it[c] === "number" ? fmtNum(it[c]) : it[c])}</td>`).join("")}</tr>`).join("")}</tbody>`
    : `<tbody><tr><td class="muted">${t("data.empty")}</td></tr></tbody>`;
  showFresh("fr-browse", r.freshness);
  $("#browse-prev").disabled = B.offset === 0;
  $("#browse-next").disabled = B.offset + PAGE >= r.total;
  $("#browse").scrollIntoView({ behavior: "smooth", block: "start" });
}
$("#browse-prev").onclick = () => { S.browse.offset = Math.max(0, S.browse.offset - PAGE); browse(); };
$("#browse-next").onclick = () => { S.browse.offset += PAGE; browse(); };
let qTimer; $("#browse-q").oninput = (e) => { clearTimeout(qTimer); qTimer = setTimeout(() => { S.browse.q = e.target.value.trim(); S.browse.offset = 0; browse(); }, 300); };

$("#ev-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const body = Object.fromEntries([...new FormData(ev.target)].filter(([, v]) => v !== ""));
  for (const k of ["lat", "lon"]) if (body[k]) body[k] = +body[k];
  if (body.heads) body.heads = parseInt(body.heads, 10);
  try { const r = await api("/api/events", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("#ev-msg").textContent = t("f.saved", { id: r.id }); ev.target.reset(); viewData(); lastCollect(); }
  catch (e) { $("#ev-msg").textContent = t("f.err", { m: e.message }); }
};


/* ── 역대 분석 ──────────────────────────────────────────────────────── */
const heatColor = (v, max) => {
  if (!v) return "var(--soft)";
  const a = 0.12 + 0.88 * Math.sqrt(v / Math.max(1, max));
  return `rgba(180,35,24,${a.toFixed(2)})`;
};
const heatCell = (v, max) => `<td style="background:${heatColor(v, max)};color:${v && v / max > 0.45 ? "#fff" : "inherit"}">${v || ""}</td>`;

async function viewHist() {
  if (S.species === "all") { S.species = "poultry"; renderSpeciesTabs(); }
  const sp = S.species, cfg = CFG.species[sp];
  const dsel = $("#h-disease");
  const names = S.lang === "en" ? cfg.diseases_en : cfg.diseases;
  if (dsel.dataset.sp !== sp || dsel.dataset.lang !== S.lang) {
    dsel.innerHTML = Object.entries(names).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("") + `<option value="all">${t("tl.alld")}</option>`;
    dsel.value = S.hDisease && (S.hDisease in names || S.hDisease === "all") ? S.hDisease : cfg.main_disease;
    dsel.dataset.sp = sp; dsel.dataset.lang = S.lang;
  }
  const q = `species=${sp}&disease=${dsel.value}`;
  const [se, mo, di, rg, rp] = await Promise.all(["seasons", "months", "diseases", "regions", "repeats"].map((k) => api(`/api/history/${k}?${q}`)));
  drawSeasons(se); drawMonths(mo); drawDiseases(di); drawRegions(rg); drawRepeats(rp);
  api(`/api/seasons?species=${sp}&disease=${dsel.value}`).then((x) => drawNetworks(x.items)).catch(() => {});
  api("/api/birds").then(drawBirdCharts).catch(() => {});
  showFresh("fr-h", se.freshness);
  // 차량 방문 비교는 계산이 길 수 있어 따로
  $("#h-v-note").textContent = t("h.visits.loading");
  api(`/api/history/visits?${q}`).then(drawVisitCC).catch(() => { $("#h-v-note").textContent = t("h.visits.none"); });
}
$("#h-disease").onchange = (e) => { S.hDisease = e.target.value; viewHist(); };

function drawSeasons(se) {
  const n = se.series.length;
  const maxW = Math.max(0, ...se.series.map((s) => s.cum.length));
  const labels = Array.from({ length: maxW }, (_, w) => w);
  const sm = se.season_start_month;
  const wkLabel = (w) => { const d = new Date(2001, sm - 1, 1 + w * 7); return `${d.getMonth() + 1}/${d.getDate()}`; };
  chart("chart-h-seasons", { type: "line", data: { labels: labels.map(wkLabel), datasets: se.series.map((s, i) => {
    const last = i === n - 1, prev = i === n - 2;
    return { label: s.label + (se.rows[i].in_progress ? ` (${t("h.now")})` : ""), data: s.cum, pointRadius: 0, tension: 0.15,
      borderWidth: last ? 3 : prev ? 2 : 1.5,
      borderColor: last ? "#B42318" : prev ? "#0E5A55" : `rgba(110,118,113,${0.25 + 0.5 * i / Math.max(1, n - 1)})` };
  }) }, options: { plugins: { cursorLine: false, tooltip: { mode: "index", intersect: false,
      itemSort: (a, b) => b.raw - a.raw } }, interaction: { mode: "index", intersect: false },
    scales: { y: { beginAtZero: true, title: { display: true, text: t("h.cum") } }, x: { title: { display: true, text: t("h.weekof") }, ticks: { maxTicksLimit: 13 } } } } });
  const cur = se.rows.find((r) => r.label === se.current);
  $("#h-s-note").textContent = t(cur ? "h.s.note" : "h.s.note0", { s: se.current, n: cur ? cur.by_now : 0, w: se.now_week + 1 });
  const cols = [["label", "h.c.season"], ["outbreaks", "h.c.n"], ["by_now", "h.c.bynow"], ["first", "h.c.first"], ["peak_date", "h.c.peak"],
    ["peak_n", "h.c.peakn"], ["last", "h.c.last"], ["duration", "h.c.dur"], ["sido", "h.c.sido"], ["sigungu", "h.c.sgg"], ["heads", "h.c.heads"]];
  const rows = [...se.rows].reverse();
  $("#h-s-tbl").innerHTML = `<thead><tr>${cols.map(([, k]) => `<th class="${k === "h.c.season" ? "" : "r"}"${tipA(k)}>${t(k)}</th>`).join("")}</tr></thead><tbody>${rows.map((r) =>
    `<tr data-y="${r.year}">${cols.map(([c]) => `<td class="${c === "label" ? "" : "r"}">${c === "label" ? `<b>${esc(r.label)}</b>${r.in_progress ? ` <span class="tag">${t("h.now")}</span>` : ""}`
      : typeof r[c] === "number" ? fmtNum(r[c]) : esc(r[c] ?? "–")}</td>`).join("")}</tr>`).join("")}</tbody>`;
  // 행을 누르면 그 시즌을 발생 추적(재생)으로
  $("#h-s-tbl").querySelectorAll("tbody tr").forEach((tr) => tr.onclick = () => {
    S.tlSeason = +tr.dataset.y; S.tlDisease = $("#h-disease").value; document.querySelector('#nav button[data-view="track"]').click(); });
}

function drawMonths(mo) {
  const max = Math.max(1, ...mo.rows.flatMap((r) => r.counts));
  $("#h-months").innerHTML = `<thead><tr><th></th>${Array.from({ length: 12 }, (_, i) => `<th>${i + 1}</th>`).join("")}<th class="r">${t("h.c.n")}</th></tr></thead>
    <tbody>${[...mo.rows].reverse().map((r) => `<tr><th>${r.year}</th>${r.counts.map((v) => heatCell(v, max)).join("")}<td class="r">${fmtNum(r.total)}</td></tr>`).join("")}
    <tr class="sum"><th>${t("h.sum")}</th>${mo.month_total.map((v) => `<td>${fmtNum(v)}</td>`).join("")}<td></td></tr></tbody>`;
}

function drawDiseases(di) {
  const max = Math.max(1, ...di.rows.flatMap((r) => r.counts));
  const dn = (d) => (S.lang === "en" ? CFG.species[S.species].diseases_en : CFG.species[S.species].diseases)[d] || d;
  $("#h-diseases").innerHTML = `<thead><tr><th></th>${di.years.map((y) => `<th>${String(y).slice(2)}</th>`).join("")}<th class="r">${t("h.c.n")}</th></tr></thead>
    <tbody>${di.rows.map((r) => `<tr><th title="${esc(r.disease)}">${esc(dn(r.disease))}</th>${r.counts.map((v) => heatCell(v, max)).join("")}<td class="r">${fmtNum(r.total)}</td></tr>`).join("")}</tbody>`;
}

function drawRegions(rg) {
  const max = Math.max(1, ...rg.rows.flatMap((r) => r.counts));
  $("#h-regions").innerHTML = `<thead><tr><th></th>${rg.seasons.map((s) => `<th>${esc(s.label)}</th>`).join("")}<th class="r"${tipA("h.c.hit")}>${t("h.c.hit")}</th><th class="r">${t("h.c.n")}</th></tr></thead>
    <tbody>${rg.rows.map((r) => `<tr><th>${esc(r.region)}</th>${r.counts.map((v) => heatCell(v, max)).join("")}<td class="r">${r.seasons_hit}</td><td class="r">${fmtNum(r.total)}</td></tr>`).join("")
    || `<tr><td class="muted">${t("data.empty")}</td></tr>`}</tbody>`;
}

function drawRepeats(rp) {
  $("#h-r-note").textContent = t("h.r.note", { n: fmtNum(rp.total) });
  $("#h-repeats").innerHTML = `<thead><tr><th>${t("h.c.place")}</th><th class="r"${tipA("h.c.ns")}>${t("h.c.ns")}</th><th class="r">${t("h.c.n")}</th><th>${t("h.c.seasons")}</th></tr></thead>
    <tbody>${rp.rows.map((r) => `<tr><td>${esc(r.place)}</td><td class="r"><b>${r.n_seasons}</b></td><td class="r">${r.events}</td><td class="muted small">${r.seasons.map(esc).join(", ")}</td></tr>`).join("")
    || `<tr><td class="muted">${t("data.empty")}</td></tr>`}</tbody>`;
}

function drawVisitCC(v) {
  if (!v.coverage) { $("#h-v-note").textContent = t("h.visits.none"); S.charts["chart-h-visits"]?.destroy(); return; }
  $("#h-v-note").textContent = t("h.visits.note", { a: v.coverage.from, b: v.coverage.to, d: v.coverage.days, c: v.cases, k: v.candidates });
  const rows = v.rows.filter((r) => r.ratio != null).slice(0, 12);
  chart("chart-h-visits", { type: "bar", data: { labels: rows.map((r) => r.vehicle), datasets: [{ label: t("h.ratio"), data: rows.map((r) => r.ratio),
    backgroundColor: rows.map((r) => r.ratio >= 1 ? "#B42318" : "#8A8F8C"), borderRadius: 3 }] },
    options: { indexAxis: "y", plugins: { legend: { display: false }, cursorLine: false,
      tooltip: { callbacks: { label: (it) => { const r = rows[it.dataIndex]; return t("h.ratio.tip", { r: r.ratio, c: r.case_per_day, b: r.base_per_day }); } } } },
      scales: { x: { beginAtZero: true, title: { display: true, text: t("h.ratio") } } } },
    plugins: [{ id: "ref1", afterDatasetsDraw(c) {      // 배율 1(평소와 같음) 기준선
      const x = c.scales.x.getPixelForValue(1), { top, bottom } = c.chartArea, g = c.ctx;
      g.save(); g.strokeStyle = "#17201B"; g.setLineDash([4, 3]); g.beginPath(); g.moveTo(x, top); g.lineTo(x, bottom); g.stroke(); g.restore(); } }] });
}

/* ── 뉴스·공지 ─────────────────────────────────────────────────────── */
const NV = { kind: "", q: "", rel: true, offset: 0, size: 50 };
const lastSeen = () => { try { return localStorage.getItem("ldt.newsSeen") || ""; } catch { return ""; } };
const relTime = (iso) => {
  if (!iso) return "";
  const m = Math.round((Date.now() - Date.parse(iso)) / 60000);
  return m < 1 ? t("nv.now") : m < 60 ? t("nv.min", { n: m }) : m < 1440 ? t("nv.hour", { n: Math.round(m / 60) }) : String(iso).slice(0, 10);
};
const spTags = (s) => (s || "").split(",").filter(Boolean).map((x) => `<span class="tag sp-${x}">${esc(spLabel(x))}</span>`).join(" ");
const newsLi = (n, seen) => `<li class="${seen && n.fetched_at > seen ? "new" : ""}"><span class="d" title="${esc(fmtTime(n.published || n.fetched_at))}">${relTime(n.published || n.fetched_at)}</span>
  <span><a href="${esc(n.link)}" target="_blank" rel="noopener noreferrer">${esc(n.title)}</a><br>
  <span class="muted">${n.kind === "notice" ? `<span class="tag k-notice">${t("nv.noticeTag")}</span> ` : ""}${esc(n.publisher || "")}</span> ${spTags(n.species)}</span></li>`;

async function viewNews() {
  const r = await api(`/api/news?${new URLSearchParams({ kind: NV.kind, q: NV.q, relevant: NV.rel ? 1 : 0, limit: NV.size, offset: NV.offset,
    species: S.species === "all" ? "" : S.species })}`);
  const seen = lastSeen();
  $("#nv-list").innerHTML = r.items.map((n) => newsLi(n, seen)).join("") || `<li class="muted">${t("nv.empty")}</li>`;
  $("#nv-count").textContent = r.total ? t("data.count", { a: NV.offset + 1, b: Math.min(NV.offset + NV.size, r.total), n: fmtNum(r.total) }) : "";
  $("#nv-prev").disabled = NV.offset === 0; $("#nv-next").disabled = NV.offset + NV.size >= r.total;
  const st = r.status || {};
  $("#nv-status").textContent = [st.news ? t("nv.st.news", { t: fmtTime(st.news.at), n: st.news.new ?? 0 }) + (st.news.ok === false ? " ⚠" : "") : "",
    st.notice ? t("nv.st.notice", { t: fmtTime(st.notice.at), n: st.notice.new ?? 0 }) + (st.notice.ok === false ? " ⚠" : "") : "",
    t("nv.every", { a: Math.round(r.every.news / 60), b: Math.round(r.every.notice / 60) })].filter(Boolean).join(" · ");
  // 일별 건수(최근 30일)
  const days = [...new Set(r.daily.map((d) => d.d))].sort().slice(-30);
  chart("chart-news", { type: "bar", data: { labels: days.map((d) => d.slice(5)), datasets: ["news", "notice"].map((k, i) => ({
    label: t("nv." + k), data: days.map((d) => r.daily.filter((x) => x.d === d && x.kind === k).reduce((a, x) => a + x.n, 0)),
    backgroundColor: i ? "#B54708" : "#0E5A55", borderRadius: 2 })) },
    options: { plugins: { cursorLine: false }, scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } } } } });
  try { localStorage.setItem("ldt.newsSeen", new Date().toISOString()); } catch {}
  $("#news-badge").hidden = true;
}
document.querySelectorAll("#nv-kind button").forEach((b) => b.onclick = () => {
  NV.kind = b.dataset.k; NV.offset = 0; document.querySelectorAll("#nv-kind button").forEach((x) => x.classList.toggle("on", x === b)); viewNews(); });
let nvT; $("#nv-q").oninput = (e) => { clearTimeout(nvT); nvT = setTimeout(() => { NV.q = e.target.value.trim(); NV.offset = 0; viewNews(); }, 350); };
$("#nv-rel").onchange = (e) => { NV.rel = e.target.checked; NV.offset = 0; viewNews(); };
$("#nv-prev").onclick = () => { NV.offset = Math.max(0, NV.offset - NV.size); viewNews(); };
$("#nv-next").onclick = () => { NV.offset += NV.size; viewNews(); };
$("#nv-refresh").onclick = async () => { $("#nv-refresh").disabled = true; try { await api("/api/news/refresh", { method: "POST" }); } catch {} $("#nv-refresh").disabled = false; viewNews(); };
$("#dash-news-more").onclick = () => document.querySelector('#nav button[data-view="news"]').click();

async function dashNews() {
  const r = await api(`/api/news?limit=7&species=${S.species === "all" ? "" : S.species}`).catch(() => null);
  if (!r) return;
  const seen = lastSeen();
  $("#dash-news").innerHTML = r.items.map((n) => newsLi(n, seen)).join("") || `<li class="muted">${t("nv.empty")}</li>`;
}
// 1분마다 새 소식 수를 메뉴 배지로
async function pollNews() {
  const seen = lastSeen();
  if (S.view !== "news") {
    const r = await api(`/api/news?limit=1${seen ? "&since=" + encodeURIComponent(seen) : ""}`).catch(() => null);
    if (r && S.view !== "news") { $("#news-badge").hidden = !r.total || !seen; $("#news-badge").textContent = r.total > 99 ? "99+" : r.total; }
    if (S.view === "dash") dashNews();
  }
}
setInterval(pollNews, 60000);

/* ── 시즌 전파 연결망(두 시즌 나란히) ──────────────────────────────── */
async function drawNetworks(seasons) {
  const sp = S.species, dis = $("#h-disease").value;
  const opts = seasons.map((x) => `<option value="${x.year}">${esc(x.label)} (${fmtNum(x.events)})</option>`).join("");
  for (const [id, idx] of [["net-a", 1], ["net-b", 0]]) {
    const sel = $("#" + id); const prev = sel.value;
    sel.innerHTML = opts; sel.value = seasons.some((x) => String(x.year) === prev) ? prev : (seasons[Math.min(idx, seasons.length - 1)]?.year ?? "");
    sel.onchange = () => drawNet(id, sp, dis);
    await drawNet(id, sp, dis);
  }
}
async function drawNet(id, sp, dis) {
  const year = $("#" + id).value, key = "net-" + id;
  const m = S.maps[key] ||= await makeMap($("#map-" + id), { base: "gray", zoom: 6 });
  if (!year) { m.dots("ev", []); m.lines?.("links", []); return; }
  const d = await api(`/api/season?species=${sp}&disease=${dis}&year=${year}`);
  const by = Object.fromEntries((d.events || []).map((e) => [e.id, e]));
  const links = (d.events || []).filter((e) => e.linked && by[e.prev?.id]);
  m.lines?.("links", links.map((e) => ({ from: [by[e.prev.id].lat, by[e.prev.id].lon], to: [e.lat, e.lon], color: "#C8102E", opacity: 0.75,
    title: `${by[e.prev.id].date} → ${e.date} · ${e.prev.km}km · ${e.prev.days}${t("unit.day")}` })));
  m.dots("ev", (d.events || []).map((e) => ({ ...e, r: 3, color: e.linked ? "#C8102E" : "#6D28D9", opacity: 0.85, weight: 0.5, z: 5,
    title: `${e.date} ${e.place || ""}${e.prev ? ` · ${t("tl.prevshort", { km: e.prev.km, d: e.prev.days })}` : ""}` })));
  const s = d.summary || {};
  const len = links.reduce((a, e) => a + (e.prev.km || 0), 0);
  const far = Math.max(0, ...(d.events || []).map((e) => e.dist_index_km || 0));
  $("#" + id + "-sum").innerHTML = t("net.sum", { s: d.label, n: fmtNum(s.events || 0), l: fmtNum(links.length), km: fmtNum(Math.round(len)),
    c: s.clusters || 0, f: fmtNum(Math.round(far)) });
}

/* ── 철새 이동 벡터(검역본부 위치추적 북상 기록) ─────────────────────── */
const BIRD_PAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6"];
const BIRD = { year: "", species: "" };
async function drawBirds(m, on) {
  const box = $("#bird-legend");
  if (!on) { m.arrows("birds", []); m.dots("birdsrc", []); if (box) box.innerHTML = ""; return; }
  const r = await api(`/api/birds?${new URLSearchParams({ year: BIRD.year, species: BIRD.species })}`).catch(() => null);
  if (!r || !r.items.length) { m.arrows("birds", []); if (box) box.innerHTML = `<span class="muted">${t("bird.none")}</span>`; return; }
  const top = r.species.slice(0, BIRD_PAL.length).map((x) => x.species);
  const col = (sp) => { const i = top.indexOf(sp); return i >= 0 ? BIRD_PAL[i] : "#8A8F8C"; };
  const items = r.items.filter((x) => x.origin_lat != null && x.dest_lat != null);
  m.arrows("birds", items.map((x) => ({ from: [x.origin_lat, x.origin_lon], to: [x.dest_lat, x.dest_lon], color: col(x.species), start: true,
    opacity: x.dest_precision === "country" ? 0.45 : 0.85, weight: 1.8,
    title: t("bird.tip", { sp: x.species, tag: x.tag, d: x.depart || "", o: x.origin, t: x.dest, km: fmtNum(Math.round(x.distance_km)),
      c: S.lang === "en" ? x.compass_en : x.compass, deg: Math.round(x.bearing), h: x.clock,
      p: x.dest_precision === "country" ? t("bird.prec.country") : x.dest_precision === "province" ? t("bird.prec.province") : "" }) })));
  if (!S._birdFit) { S._birdFit = true; m.fitLatLngs(items.flatMap((x) => [[x.origin_lat, x.origin_lon], [x.dest_lat, x.dest_lon]]), 6); }
  if (box) {
    box.innerHTML = `<span class="muted" data-tip="tip.lyr.birds">${t("bird.legend", { n: items.length, km: fmtNum(Math.round(r.median_km || 0)) })}</span>
      <select id="bird-year"><option value="">${t("bird.allyears")}</option>${r.years.map((y) => `<option ${String(y) === String(BIRD.year) ? "selected" : ""}>${y}</option>`).join("")}</select>` +
      r.species.slice(0, BIRD_PAL.length).map((x, i) => `<button class="sw chip ${BIRD.species === x.species ? "on" : ""}" data-sp="${esc(x.species)}"><i style="background:${BIRD_PAL[i]}"></i>${esc(x.species)} ${x.n}</button>`).join("") +
      (r.species.length > BIRD_PAL.length ? `<span class="sw"><i style="background:#8A8F8C"></i>${t("bird.other")}</span>` : "");
    $("#bird-year").onchange = (e) => { BIRD.year = e.target.value; drawBirds(m, true); };
    box.querySelectorAll("button[data-sp]").forEach((b) => b.onclick = () => { BIRD.species = BIRD.species === b.dataset.sp ? "" : b.dataset.sp; drawBirds(m, true); });
  }
}

function drawBirdCharts(r) {
  if (!r.items.length) { $("#bird-sum").textContent = t("bird.none"); return; }
  $("#bird-sum").textContent = Object.entries(r.by_country).sort((a, b) => b[1] - a[1]).map(([c, n]) => `${c} ${n}`).join(" · ");
  const labels = S.lang === "en" ? r.compass_en : r.compass;
  chart("chart-bird-rose", { type: "polarArea", data: { labels, datasets: [{ data: r.rose,
    backgroundColor: r.rose.map((_, i) => i <= 2 || i >= 14 ? "rgba(42,120,214,0.75)" : "rgba(42,120,214,0.35)"), borderColor: "#fff", borderWidth: 1 }] },
    options: { startAngle: -11.25, plugins: { legend: { display: false }, cursorLine: false },
      scales: { r: { ticks: { display: false }, pointLabels: { display: true, centerPointLabels: true, font: { size: 11 } } } } } });
  // 연도별 북상일(월·일) 점: 가로 = 날짜(1/1 기준 일수), 세로 = 연도
  const doy = (d) => { const x = new Date(d); return Math.round((x - new Date(x.getFullYear(), 0, 1)) / 864e5); };
  const top = r.species.slice(0, BIRD_PAL.length).map((x) => x.species);
  const groups = [...top, "_other"].map((sp, i) => ({ label: sp === "_other" ? t("bird.other") : sp, backgroundColor: i < top.length ? BIRD_PAL[i] : "#8A8F8C",
    pointRadius: 4, data: r.items.filter((x) => x.depart && (sp === "_other" ? !top.includes(x.species) : x.species === sp))
      .map((x) => ({ x: doy(x.depart), y: +x.year + (Math.random() - 0.5) * 0.3, d: x })) })).filter((g) => g.data.length);
  const md = (v) => { const x = new Date(2001, 0, 1 + v); return `${x.getMonth() + 1}/${x.getDate()}`; };
  chart("chart-bird-time", { type: "scatter", data: { datasets: groups },
    options: { plugins: { cursorLine: false, tooltip: { callbacks: { label: (it) => `${it.raw.d.species} · ${it.raw.d.depart} · ${it.raw.d.origin} → ${it.raw.d.dest}` } } },
      scales: { x: { min: 0, max: 160, ticks: { callback: md, stepSize: 15 } }, y: { ticks: { stepSize: 1, precision: 0, callback: (v) => String(v) } } } } });
}

/* ── 기상 관측소 기온 점(상황판) ─────────────────────────────────────── */
const WX_BINS = [[-99, -5, "#3B6FB6"], [-5, -3, "#6FA8C8"], [-3, 0, "#A6D4C8"], [0, 10, "#E8F0B0"], [10, 20, "#F6D36B"], [20, 30, "#EE9A3A"], [30, 99, "#C8442B"]];
const wxColor = (v) => v == null ? "#5A2A27" : (WX_BINS.find(([a, b]) => v >= a && v < b) || WX_BINS[WX_BINS.length - 1])[2];
async function drawWxMap(m, on) {
  if (!on) { m.dots("wx", []); $("#wx-legend").innerHTML = ""; return; }
  const w = await api("/api/weather/latest").catch(() => null);
  if (!w || !w.items.length) { m.dots("wx", []); $("#wx-legend").innerHTML = `<span class="muted">${t("wx.none")}</span>`; return; }
  m.dots("wx", w.items.map((x) => ({ ...x, r: 6, color: wxColor(x.temp_avg), stroke: "#17201B", weight: 1, opacity: 0.95, z: 4,
    title: `${x.name} · ${t("wx.tip", { d: w.date, t: x.temp_avg ?? "–", lo: x.temp_min ?? "–", h: x.humidity_avg ?? "–" })}` })));
  $("#wx-legend").innerHTML = `<span class="muted" data-tip="tip.lyr.wx">${t("wx.legend", { d: w.date })}</span>` +
    WX_BINS.map(([a, b, c], i) => `<span class="sw"><i style="background:${c}"></i>${i === 0 ? "≤" + b : i === WX_BINS.length - 1 ? a + "+" : a + "~" + b}</span>`).join("");
}

/* ── 4. 데이터 조회: 원천(API)별로 수집된 데이터 ─────────────────────────── */
const XP = (() => { try { return JSON.parse(localStorage.getItem("ldt.xp") || "null") || {}; } catch { return {}; } })();
Object.assign(XP, { tab: XP.tab || "table", size: XP.size || 100, offset: 0, filters: {}, q: "", from: "", to: "", sort: null, desc: true });
const xpSave = () => { try { localStorage.setItem("ldt.xp", JSON.stringify({ sid: XP.sid, tab: XP.tab, size: XP.size, hideEmpty: XP.hideEmpty })); } catch {} };
const PROV = { mafra: "xp.g.mafra", datagokr: "xp.g.datagokr", odcloud: "xp.g.datagokr", kma_asos: "xp.g.kma",
  kahis_visit: "xp.g.kahis", file: "xp.g.file" };
const xpQs = (extra = {}) => {
  const p = new URLSearchParams();
  if (XP.q) p.set("q", XP.q);
  if (XP.from) p.set("from", XP.from);
  if (XP.to) p.set("to", XP.to);
  Object.entries(XP.filters).forEach(([k, v]) => v !== "" && p.set("f_" + k, v));
  Object.entries(extra).forEach(([k, v]) => v != null && p.set(k, v));
  return p.toString();
};
const xcol = (c) => { const k = "xc." + c, v = t(k); return v === k ? c : v; };
const xpCell = (v, type) => v == null || v === "" ? `<span class="muted">–</span>`
  : type === "num" ? fmtNum(v) : type === "time" ? esc(fmtTime(v)) : esc(String(v).length > 120 ? String(v).slice(0, 120) + "…" : v);

async function viewExplore() {
  const cat = await api("/api/explore");
  XP.cat = cat.sources;
  $("#xp-hide-empty").checked = !!XP.hideEmpty;
  renderXpList();
  if (XP.sid && XP.cat.some((x) => x.id === XP.sid)) await xpOpen(XP.sid, true);
}

function renderXpList() {
  const f = ($("#xp-find").value || "").trim().toLowerCase();
  const items = XP.cat.filter((x) => (!XP.hideEmpty || x.rows > 0) &&
    (!f || (x.title + x.title_en + x.id).toLowerCase().includes(f)));
  const groups = {};
  items.forEach((x) => (groups[PROV[x.provider] || "xp.g.etc"] ||= []).push(x));
  $("#xp-list").innerHTML = Object.entries(groups).map(([g, list]) => `<div class="xp-g">${t(g)}</div>` + list.map((x) => {
    const st = x.last && !x.last.ok ? "bad" : x.rows > 0 ? "ok" : x.configured ? "idle" : "off";
    return `<button class="xp-item ${x.id === XP.sid ? "on" : ""}" data-sid="${x.id}" title="${esc(x.id)}">
      <span class="dot ${st}"></span><span class="nm">${esc(S.lang === "en" ? x.title_en : x.title)}</span>
      <span class="n">${x.rows ? fmtNum(x.rows) : x.configured ? "0" : t("xp.notset")}</span></button>`;
  }).join("")).join("") || `<p class="muted pad">${t("xp.none")}</p>`;
  $("#xp-list").querySelectorAll(".xp-item").forEach((b) => b.onclick = () => xpOpen(b.dataset.sid));
}
$("#xp-find").oninput = () => XP.cat && renderXpList();
$("#xp-hide-empty").onchange = (e) => { XP.hideEmpty = e.target.checked; xpSave(); renderXpList(); };

async function xpOpen(sid, keep = false) {
  if (!keep || XP.sid !== sid) Object.assign(XP, { offset: 0, filters: {}, q: "", from: "", to: "", sort: null, desc: true });
  XP.sid = sid; xpSave();
  // 축산차량 방문은 행이 아주 많아 처음엔 최근 30일만
  if (sid === "kahis_vehicle_visit" && !XP.from && !XP.to && (!keep || !XP._vvInit)) {
    const x0 = XP.cat.find((c) => c.id === sid);
    if (x0?.data_to) { const d = new Date(x0.data_to); d.setDate(d.getDate() - 29); XP.from = d.toISOString().slice(0, 10); XP.to = x0.data_to; }
    XP._vvInit = true;
  }
  const x = XP.cat.find((c) => c.id === sid) || {};
  $("#xp-list").querySelectorAll(".xp-item").forEach((b) => b.classList.toggle("on", b.dataset.sid === sid));
  $("#xp-title").textContent = S.lang === "en" ? x.title_en : x.title;
  const last = x.last ? `${t("xp.last")} ${fmtTime(x.last.finished_at)} ${x.last.ok ? "" : `<b class="bad">${esc((x.last.message || "").slice(0, 120))}</b>`}` : t("xp.never");
  $("#xp-meta").innerHTML = [`<code>${esc(x.id)}</code>`, esc(x.target), x.service ? `<span title="${esc(x.service)}">${esc(String(x.service).replace(/\?.*$/, "").slice(-48))}</span>` : "",
    x.data_from ? `${x.data_from} ~ ${x.data_to}` : "", t("xp.rows", { n: fmtNum(x.rows), r: fmtNum(x.raw) }), last].filter(Boolean).join(" · ");
  $("#xp-q").value = XP.q; $("#xp-from").value = XP.from; $("#xp-to").value = XP.to; $("#xp-size").value = XP.size;
  $("#xp-body").hidden = false;
  await xpLoad();
}

async function xpLoad() {
  document.querySelectorAll("#xp-tabs button").forEach((b) => b.classList.toggle("on", b.dataset.t === XP.tab));
  const isTable = XP.tab === "table";
  document.querySelector(".xp-dates").style.display = isTable ? "" : "none";
  $("#xp-facets").style.display = isTable ? "" : "none";
  $("#xp-csv").style.display = isTable ? "" : "none";
  $("#xp-chart-wrap").style.display = isTable ? "" : "none";
  $("#xp-pager-hide")?.remove();
  document.querySelector(".xp-pager").style.display = "";
  $("#xp-prev").style.display = $("#xp-next").style.display = $("#xp-size").style.display = XP.tab === "profile" ? "none" : "";
  if (XP.tab === "table") return xpTable();
  if (XP.tab === "raw") return xpRaw();
  return xpProfile();
}

async function xpTable() {
  const sid = XP.sid;
  let r;
  try { r = await api(`/api/explore/${sid}/table?${xpQs({ sort: XP.sort, desc: XP.desc ? 1 : 0, limit: XP.size, offset: XP.offset })}`); }
  catch (e) {   // 이 원천을 표로 못 보여 줄 때: 이전 원천의 필터·그래프가 남지 않게 비움
    if (sid !== XP.sid) return;
    $("#xp-facets").innerHTML = ""; $("#xp-tbl").innerHTML = ""; $("#xp-chart-wrap").style.display = "none";
    $("#xp-count").textContent = t("xp.err", { m: e.message }); return;
  }
  if (sid !== XP.sid) return;
  XP.cols = r.columns;
  $("#xp-count").textContent = r.total ? t("data.count", { a: fmtNum(XP.offset + 1), b: fmtNum(Math.min(XP.offset + XP.size, r.total)), n: fmtNum(r.total) }) : t("data.empty");
  $("#xp-prev").disabled = XP.offset === 0; $("#xp-next").disabled = XP.offset + XP.size >= r.total;
  $("#xp-csv").href = `/api/explore/${sid}/csv?${xpQs({ sort: XP.sort, desc: XP.desc ? 1 : 0 })}`;
  document.querySelector(".xp-dates").style.display = r.has_date ? "" : "none";
  $("#xp-tbl").innerHTML = `<thead><tr>${r.columns.map((c) => `<th data-c="${c.name}" class="${c.type === "num" ? "r" : ""} ${XP.sort === c.name ? "sorted" : ""}" title="${esc(c.name)}">${esc(xcol(c.name))}${XP.sort === c.name ? (XP.desc ? " ▼" : " ▲") : ""}</th>`).join("")}</tr></thead>
    <tbody>${r.items.map((it, i) => `<tr data-i="${i}">${r.columns.map((c) => `<td class="${c.type === "num" ? "r" : ""}">${xpCell(it[c.name], c.type)}</td>`).join("")}</tr>`).join("")}</tbody>`;
  $("#xp-tbl").querySelectorAll("th").forEach((th) => th.onclick = () => {
    XP.desc = XP.sort === th.dataset.c ? !XP.desc : true; XP.sort = th.dataset.c; XP.offset = 0; xpTable(); });
  $("#xp-tbl").querySelectorAll("tbody tr").forEach((tr) => tr.onclick = () => xpDetail(r, r.items[+tr.dataset.i]));
  xpFacets(r.facets);
  xpTimeline();
}

async function xpFacets(cols) {
  const sid = XP.sid;
  const fs = await Promise.all(cols.map((c) => api(`/api/explore/${sid}/facet/${c}?${xpQs()}`).catch(() => null)));
  if (sid !== XP.sid) return;
  $("#xp-facets").innerHTML = fs.map((f, i) => f && `<label class="inline"><span class="muted">${esc(xcol(cols[i]))}</span>
    <select data-c="${cols[i]}"><option value="">${t("xp.all")} (${fmtNum(f.distinct)})</option>
    ${f.values.map((v) => `<option value="${v.v == null || v.v === "" ? "∅" : esc(v.v)}" ${String(XP.filters[cols[i]] ?? "") === String(v.v ?? "∅") ? "selected" : ""}>${esc(v.v == null || v.v === "" ? t("xp.blank") : v.v)} (${fmtNum(v.n)})</option>`).join("")}
    ${XP.filters[cols[i]] && !f.values.some((v) => String(v.v) === XP.filters[cols[i]]) ? `<option selected value="${esc(XP.filters[cols[i]])}">${esc(XP.filters[cols[i]])}</option>` : ""}</select></label>`).join("");
  $("#xp-facets").querySelectorAll("select").forEach((sel) => sel.onchange = () => {
    if (sel.value) XP.filters[sel.dataset.c] = sel.value; else delete XP.filters[sel.dataset.c];
    XP.offset = 0; xpTable(); });
}

async function xpTimeline() {
  const sid = XP.sid;
  const tl = await api(`/api/explore/${sid}/timeline?${xpQs()}`).catch(() => ({ items: [] }));
  if (sid !== XP.sid) return;
  $("#xp-chart-wrap").style.display = tl.items.length ? "" : "none";
  if (!tl.items.length) return;
  chart("chart-xp", { type: "bar", data: { labels: tl.items.map((x) => x.k), datasets: [{ label: t("xp.unit." + tl.unit), data: tl.items.map((x) => x.n),
    backgroundColor: "#0E5A55", borderRadius: 2 }] },
    options: { plugins: { legend: { display: false }, cursorLine: false }, scales: { y: { beginAtZero: true }, x: { ticks: { maxTicksLimit: 14 } } },
      onClick: (_, els) => { if (!els.length) return; const k = tl.items[els[0].index].k;
        const [a, b] = tl.unit === "year" ? [`${k}-01-01`, `${k}-12-31`] : tl.unit === "month" ? [`${k}-01`, `${k}-${String(new Date(+k.slice(0, 4), +k.slice(5, 7), 0).getDate()).padStart(2, "0")}`] : [k, k];
        XP.from = a; XP.to = b; $("#xp-from").value = a; $("#xp-to").value = b; XP.offset = 0; xpTable(); } } });
}

async function xpDetail(r, row) {
  $("#xp-drawer").hidden = false;
  $("#xp-d-title").textContent = r.title + (row.id ? ` #${row.id}` : "");
  let raw = null;
  if (r.has_raw && row.id) raw = await api(`/api/explore/record/${r.table}/${row.id}`).catch(() => null);
  const kv = (o) => `<table class="tbl kv">${Object.entries(o).filter(([k]) => k !== "raw").map(([k, v]) =>
    `<tr><th title="${esc(k)}">${esc(xcol(k))}</th><td>${v == null ? "–" : esc(typeof v === "object" ? JSON.stringify(v) : v)}</td></tr>`).join("")}</table>`;
  $("#xp-d-body").innerHTML = kv(row) + (raw?.raw ? `<h3 class="xp-h3">${t("xp.rawof")}</h3>${kv(raw.raw)}` : "");
}
$("#xp-d-close").onclick = () => { $("#xp-drawer").hidden = true; };

async function xpRaw() {
  const sid = XP.sid;
  const r = await api(`/api/explore/${sid}/raw?${new URLSearchParams({ q: XP.q, limit: XP.size, offset: XP.offset })}`);
  if (sid !== XP.sid) return;
  $("#xp-count").textContent = r.total ? t("xp.rawcount", { a: fmtNum(XP.offset + 1), b: fmtNum(Math.min(XP.offset + XP.size, r.total)), n: fmtNum(r.total) })
    + (r.batches.length ? " · " + t("xp.batches", { b: r.batches.slice(0, 3).map((b) => `${fmtTime(b.at)} ${fmtNum(b.n)}`).join(", ") }) : "")
    : t(sid === "kahis_vehicle_visit" ? "xp.noraw.visit" : "xp.noraw");
  $("#xp-prev").disabled = XP.offset === 0; $("#xp-next").disabled = XP.offset + XP.size >= r.total;
  $("#xp-tbl").innerHTML = r.items.length ? `<thead><tr><th>#</th><th>${t("xp.fetched")}</th>${r.fields.map((f) => `<th>${esc(f)}</th>`).join("")}</tr></thead>
    <tbody>${r.items.map((it, i) => `<tr data-i="${i}"><td class="muted">${it.id}</td><td class="muted">${esc(fmtTime(it.fetched_at))}</td>${r.fields.map((f) =>
      `<td>${xpCell(typeof it.payload[f] === "object" && it.payload[f] ? JSON.stringify(it.payload[f]) : it.payload[f], "text")}</td>`).join("")}</tr>`).join("")}</tbody>` : "";
  $("#xp-tbl").querySelectorAll("tbody tr").forEach((tr) => tr.onclick = () => {
    const it = r.items[+tr.dataset.i]; $("#xp-drawer").hidden = false; $("#xp-d-title").textContent = `${t("xp.t.raw")} #${it.id}`;
    $("#xp-d-body").innerHTML = `<pre class="json">${esc(JSON.stringify(it.payload, null, 2))}</pre>`; });
}

async function xpProfile() {
  const sid = XP.sid;
  const p = await api(`/api/explore/${sid}/profile`);
  if (sid !== XP.sid) return;
  $("#xp-tbl").innerHTML = `<thead><tr><th>${t("xp.col")}</th><th>${t("xp.type")}</th><th class="r"${tipA("xp.filled")}>${t("xp.filled")}</th><th class="r"${tipA("xp.distinct")}>${t("xp.distinct")}</th><th>${t("xp.min")}</th><th>${t("xp.max")}</th></tr></thead>
    <tbody>${p.columns.map((c, i) => `<tr data-i="${i}"><td><b>${esc(xcol(c.name))}</b> <span class="muted">${esc(c.name)}</span></td><td class="muted">${c.type}</td>
      <td class="r">${p.total ? Math.round(100 * (c.filled || 0) / p.total) : 0}%<div class="fillbar"><i style="width:${p.total ? 100 * (c.filled || 0) / p.total : 0}%"></i></div></td>
      <td class="r">${fmtNum(c.distinct)}</td><td>${xpCell(c.min, c.type)}</td><td>${xpCell(c.max, c.type)}</td></tr>`).join("")}</tbody>`;
  $("#xp-count").textContent = t("xp.proftotal", { n: fmtNum(p.total) }) + (p.since ? ` · ${t("xp.since", { d: p.since })}` : "");
  $("#xp-tbl").querySelectorAll("tbody tr").forEach((tr) => tr.onclick = async () => {
    const c = p.columns[+tr.dataset.i];
    const f = await api(`/api/explore/${sid}/facet/${c.name}`);
    const max = Math.max(1, ...f.values.map((v) => v.n));
    $("#xp-drawer").hidden = false; $("#xp-d-title").textContent = t("xp.dist", { c: xcol(c.name), n: fmtNum(f.distinct) });
    $("#xp-d-body").innerHTML = `<div class="vbars">${f.values.map((v) => `<div class="vbar wide"><span title="${esc(v.v)}">${esc(v.v ?? t("xp.blank"))}</span><i style="width:${Math.max(1, 100 * v.n / max)}%"></i><b>${fmtNum(v.n)}</b></div>`).join("")}</div>`;
  });
}

document.querySelectorAll("#xp-tabs button").forEach((b) => b.onclick = () => { if (!XP.sid) return; XP.tab = b.dataset.t; XP.offset = 0; xpSave(); xpLoad(); });
let xpT; $("#xp-q").oninput = (e) => { clearTimeout(xpT); xpT = setTimeout(() => { XP.q = e.target.value.trim(); XP.offset = 0; xpLoad(); }, 350); };
$("#xp-from").onchange = (e) => { XP.from = e.target.value; XP.offset = 0; xpTable(); };
$("#xp-to").onchange = (e) => { XP.to = e.target.value; XP.offset = 0; xpTable(); };
$("#xp-size").onchange = (e) => { XP.size = +e.target.value; XP.offset = 0; xpSave(); xpLoad(); };
$("#xp-prev").onclick = () => { XP.offset = Math.max(0, XP.offset - XP.size); xpLoad(); };
$("#xp-next").onclick = () => { XP.offset += XP.size; xpLoad(); };
$("#xp-reset").onclick = () => { Object.assign(XP, { filters: {}, q: "", from: "", to: "", sort: null, desc: true, offset: 0 });
  $("#xp-q").value = ""; $("#xp-from").value = ""; $("#xp-to").value = ""; xpLoad(); };

async function refresh() {
  renderSpeciesTabs();
  try {
    await Promise.all([({ dash: viewDash, track: viewTrack, corr: viewCorr, hist: viewHist, news: viewNews, explore: viewExplore, data: viewData })[S.view](), lastCollect()]);
    Object.values(S.maps).forEach((m) => m.relayout());
  } catch (e) { console.error(e); }
}
applyLang();
refresh();
pollNews();
