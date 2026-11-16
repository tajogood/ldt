/* 보고서 — 주요 분석(지도·차트·표·결론)을 A4 쪽으로 모아 PDF로 저장(브라우저 인쇄 → PDF로 저장)
   app.js 의 도우미(api, t, chart, makeMap, forest, assoc*HTML …)를 그대로 쓴다. */

const RP_SECTIONS = ["summary", "map", "weekly", "seasons", "regions", "model", "factors", "news", "sources"];
const RP = { token: 0, on: new Set(RP_SECTIONS), ready: false, maps: [] };
try { const v = JSON.parse(localStorage.getItem("ldt.rp") || "null"); if (Array.isArray(v)) RP.on = new Set(v); } catch (_) { /* 기본값 */ }

const rpDate = () => new Date().toLocaleDateString(S.lang === "en" ? "en-CA" : "ko-KR", { year: "numeric", month: "2-digit", day: "2-digit" });
const rpIso = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
const rpStrip = (h) => String(h || "").replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim();
const rpSleep = (ms) => new Promise((r) => setTimeout(r, ms));

function rpToolbar() {
  $("#rp-sections").innerHTML = RP_SECTIONS.map((k) =>
    `<label><input type="checkbox" data-k="${k}" ${RP.on.has(k) ? "checked" : ""}> ${t("rp.s." + k)}</label>`).join("");
  $("#rp-sections").querySelectorAll("input").forEach((c) => c.onchange = () => {
    c.checked ? RP.on.add(c.dataset.k) : RP.on.delete(c.dataset.k);
    try { localStorage.setItem("ldt.rp", JSON.stringify([...RP.on])); } catch (_) { /* 무시 */ }
    buildReport();
  });
}
$("#rp-build").onclick = () => buildReport();
$("#rp-pdf").onclick = () => rpPrint();

async function viewReport() {
  rpToolbar();
  await buildReport();
}

function rpStatus(txt, busy = true) {
  $("#rp-status").innerHTML = (busy ? `<span class="spin"></span>` : "") + txt;
  $("#rp-pdf").disabled = busy;
}

// 연관 분석은 처음엔 계산 중일 수 있어 최대 3분 기다린다
async function rpAssoc(sp, dis, token) {
  for (let i = 0; i < 60; i++) {
    const d = await api(`/api/assoc?species=${sp}&disease=${dis}`).catch(() => ({ status: "error" }));
    if (token !== RP.token) return null;
    if (d.status === "ready" || d.status === "updating" || d.status === "na" || d.error || d.empty) return d;
    rpStatus(t("rp.wait.assoc"));
    await rpSleep(3000);
  }
  return { status: "timeout" };
}

const rpPage = (key, title, body, sub = "") =>
  `<section class="rp-page" data-k="${key}"><div class="rp-hd"><span>${esc(t("rp.title"))}</span><span>${esc(RP.meta)}</span></div>
   <h2 class="rp-h">${title}</h2>${sub ? `<p class="rp-sub">${sub}</p>` : ""}${body}</section>`;

async function buildReport() {
  const token = ++RP.token;
  RP.ready = false;
  if (S.species === "all") { S.species = "poultry"; renderSpeciesTabs(); }
  const sp = S.species, cfg = CFG.species[sp];
  const dis = S.tlDisease && S.tlDisease in cfg.diseases ? S.tlDisease : cfg.main_disease;
  const disName = (S.lang === "en" ? cfg.diseases_en : cfg.diseases)[dis] || dis;
  const spName = S.lang === "en" ? cfg.label_en : cfg.label;
  RP.meta = `${spName} · ${disName} · ${rpDate()}`;
  RP.file = `${t("rp.file")}_${spName.replace(/[\s()·/]+/g, "")}_${dis}_${rpIso()}`;
  rpStatus(t("rp.wait.data"));
  const q = `species=${sp}&disease=${dis}`;
  const cur = seasonRange(sp), prev = seasonRange(sp, 1);
  const on = (k) => RP.on.has(k);
  const safe = (p) => p.catch(() => null);
  const [sum, series, evCur, evPrev, se, rg, news, fr] = await Promise.all([
    safe(api(`/api/summary?species=${sp}`)), on("weekly") ? safe(api(`/api/series?species=${sp}`)) : null,
    on("map") ? safe(api(`/api/events?species=${sp}&from=${cur.from}`)) : null,
    on("map") ? safe(api(`/api/events?species=${sp}&from=${prev.from}&to=${prev.to}`)) : null,
    (on("seasons") || on("summary")) ? safe(api(`/api/history/seasons?${q}`)) : null,
    on("regions") ? safe(api(`/api/history/regions?${q}`)) : null,
    on("news") ? safe(api(`/api/news?species=${sp}&limit=12`)) : null,
    safe(api("/api/freshness"))]);
  if (token !== RP.token) return;
  const A = (on("model") || on("factors") || on("summary")) ? await rpAssoc(sp, dis, token) : null;
  if (token !== RP.token) return;
  rpStatus(t("rp.wait.draw"));

  // 지난 보고서의 지도·차트 정리
  RP.maps.forEach((m) => { try { m._el?.remove(); } catch (_) { /* 무시 */ } });
  RP.maps = [];
  Object.keys(S.charts).filter((k) => k.startsWith("rp-")).forEach(clearChart);

  const pages = [];
  const k = sum?.kpi || {};
  const okA = A && A.status !== "timeout" && !A.error && !A.empty && A.status !== "na";

  // 1. 표지·요약
  if (on("summary")) {
    const kpi = (lab, v, sub, c) => `<div class="rp-kpi"><span>${lab}</span><b style="color:${c}">${v ?? "–"}</b><small>${sub || ""}</small></div>`;
    const prevTxt = (n) => k.prev ? t("kpi.prev", { n }) : "";
    const finds = rpFindings(k, se, okA ? A : null);
    pages.push(`<section class="rp-page rp-cover" data-k="summary">
      <div class="rp-hd"><span>${esc(CFG.site?.operator || "")}</span><span>${rpDate()}</span></div>
      <h1>${esc(t("rp.title"))}</h1>
      <p class="rp-meta">${esc(spName)} · ${esc(disName)}<br>${t("rp.made", { d: rpDate() })}</p>
      <div class="rp-kpis">
        ${kpi(t("kpi.outbreaks"), k.outbreaks, `${t("kpi.since", { d: k.season_start || "" })} ${prevTxt(k.prev?.outbreaks)}`, "#B42318")}
        ${kpi(t("kpi.wild"), k.wild_detections, `${t("kpi.since", { d: k.season_start || "" })} ${prevTxt(k.prev?.wild_detections)}`, "#1D4ED8")}
        ${kpi(t("kpi.restrict"), k.restrictions_active, t("kpi.now"), "#B54708")}
        ${kpi(t("kpi.farms"), k.farms != null ? fmtNum(k.farms) : "–", k.heads ? t("kpi.headsub", { n: fmtNum(k.heads) }) : "", "#17201B")}
      </div>
      <h3>${t("rp.findings")}</h3>
      <ul class="rp-find">${finds.map((f) => `<li>${f}</li>`).join("") || `<li>${t("rp.nofind")}</li>`}</ul>
      <h3>${t("rp.toc")}</h3>
      <ol class="rp-toc">${RP_SECTIONS.filter((x) => x !== "summary" && on(x)).map((x) => `<li>${t("rp.s." + x)}</li>`).join("")}</ol>
      ${sum?.alert ? `<p class="rp-alert">${esc(sum.alert.date)} · ${esc((sum.alert.note || "").split(".")[0])}</p>` : ""}
    </section>`);
  }
  // 2. 현황 지도
  if (on("map")) {
    const nCur = (evCur?.items || []).filter((e) => e.kind === "outbreak").length, nW = (evCur?.items || []).filter((e) => e.kind === "wild_detection").length;
    pages.push(rpPage("map", t("rp.s.map"), `<div class="rp-map" id="rp-map-cur"></div>
      <p class="rp-legend"><i style="background:#B42318"></i>${t("rp.l.cur", { s: cur.label })} ${nCur} · <i style="background:#1D4ED8"></i>${t("kpi.wild")} ${nW} · <i style="background:#9AA19C"></i>${t("rp.l.prev", { s: prev.label })} ${(evPrev?.items || []).filter((e) => e.kind === "outbreak").length}</p>`,
      t("rp.map.sub")));
  }
  // 3. 주간 추이
  if (on("weekly") && series) {
    pages.push(rpPage("weekly", t("rp.s.weekly"), `<div class="rp-chart"><canvas id="rp-weekly"></canvas></div>`, t("rp.weekly.sub")));
  }
  // 4. 시즌 비교
  if (on("seasons") && se?.series?.length) {
    const rows = [...se.rows].reverse();
    pages.push(rpPage("seasons", t("rp.s.seasons"), `<div class="rp-chart"><canvas id="rp-seasons"></canvas></div>
      <table class="tbl small rp-tbl"><thead><tr><th>${t("h.c.season")}</th><th class="r">${t("h.c.n")}</th><th class="r">${t("h.c.bynow")}</th>
      <th>${t("h.c.first")}</th><th>${t("h.c.peak")}</th><th>${t("h.c.last")}</th><th class="r">${t("h.c.dur")}</th><th class="r">${t("h.c.sgg")}</th></tr></thead><tbody>` +
      rows.map((r) => `<tr><td><b>${esc(r.label)}</b>${r.in_progress ? ` (${t("h.now")})` : ""}</td><td class="r">${fmtNum(r.outbreaks)}</td><td class="r">${fmtNum(r.by_now)}</td>
        <td>${r.first || "–"}</td><td>${r.peak_date || "–"}</td><td>${r.last || "–"}</td><td class="r">${fmtNum(r.duration || 0)}</td><td class="r">${fmtNum(r.sigungu || 0)}</td></tr>`).join("") +
      `</tbody></table>`, t("rp.seasons.sub", { w: (se.now_week ?? 0) + 1 })));
  }
  // 5. 반복 발생 지역
  if (on("regions") && rg?.rows?.length) {
    const max = Math.max(1, ...rg.rows.flatMap((r) => r.counts));
    pages.push(rpPage("regions", t("rp.s.regions"), `<table class="heat rp-tbl"><thead><tr><th></th>${rg.seasons.map((s) => `<th>${esc(s.label.slice(-5))}</th>`).join("")}<th class="r">${t("h.c.n")}</th></tr></thead><tbody>` +
      rg.rows.slice(0, 20).map((r) => `<tr><th>${esc(r.region)}</th>${r.counts.map((v) => heatCell(v, max)).join("")}<td class="r">${fmtNum(r.total)}</td></tr>`).join("") +
      `</tbody></table>`, t("rp.regions.sub")));
  }
  // 6. 종합 위험 모형
  if (on("model")) {
    const M = okA ? A.model : null;
    pages.push(rpPage("model", t("as.model"), M?.available ? `
      <p class="rp-sub">${esc(assocModelSum(M))}</p>
      <div class="rp-map" id="rp-map-pred"></div><p class="rp-legend">${assocPredDots(M.pred || []).legend}</p>
      <p class="rp-sub">${(M.pred || []).length ? esc(t("as.predh", { w: M.week })) : esc(t("as.nopred"))}</p>
      <table class="tbl small rp-tbl">${assocPredTable(M.pred || [], 10)}</table>
      <h3>${t("as.or")}</h3>${assocForestHTML(M)}
      <table class="tbl small rp-tbl">${assocModelsTable(M)}</table>
      <p class="muted small">${t("as.note")}</p>` : `<p class="muted">${M ? esc(asReason(M)) : t("rp.noassoc")}</p>`));
  }
  // 7. 요인별 분석
  if (on("factors") && okA) {
    const T = A.temp || {}, B = A.birds || {};
    pages.push(rpPage("factors", t("as.wild"), assocWildHTML(A.wild || {})));
    pages.push(rpPage("factors", t("as.temp"), T.available ? `<div class="rp-chart"><canvas id="rp-temp"></canvas></div>${assocTempNote(T)}` : `<p class="muted">${asReason(T)}</p>`));
    pages.push(rpPage("factors", t("as.visits"), assocVisitsHTML(A.visits || {}, {})));
    pages.push(rpPage("factors", t("as.density"), assocDensityHTML(A.density || {}, { print: true })));
    if (sp === "poultry") pages.push(rpPage("factors", t("as.birds"), B.available ? `<div class="rp-chart"><canvas id="rp-birds"></canvas></div>${assocBirdsHTML(B)}` : `<p class="muted">${asReason(B)}</p>`));
  }
  // 8. 뉴스·공지
  if (on("news") && news?.items?.length) {
    pages.push(rpPage("news", t("rp.s.news"), `<table class="tbl small rp-tbl"><thead><tr><th>${t("th.date")}</th><th>${t("rp.news.src")}</th><th>${t("rp.news.title")}</th></tr></thead><tbody>` +
      news.items.slice(0, 12).map((n) => `<tr><td class="nowrap">${String(n.published || n.fetched_at || "").slice(0, 10)}</td><td>${esc(n.publisher || "")}</td><td>${esc(n.title)}</td></tr>`).join("") + `</tbody></table>`));
  }
  // 9. 데이터 기준·출처
  if (on("sources")) {
    const used = (fr || []).filter((f) => f.rows);
    pages.push(rpPage("sources", t("rp.s.sources"), `<table class="tbl small rp-tbl"><thead><tr><th>${t("rp.src.name")}</th><th>${t("rp.src.range")}</th><th>${t("rp.src.got")}</th><th class="r">${t("rp.src.rows")}</th></tr></thead><tbody>` +
      used.map((f) => `<tr><td>${esc((S.lang === "en" ? f.title_en : f.title) || f.source)}</td><td class="nowrap">${f.data_from ? `${f.data_from} ~ ${f.data_to || ""}` : "–"}</td><td class="nowrap">${f.collected_at ? fmtTime(f.collected_at) : "–"}</td><td class="r">${fmtNum(f.rows)}</td></tr>`).join("") +
      `</tbody></table><p class="small">${t("ft.src")}</p><p class="small">${t("ft.disc")}</p>
      <p class="small">© ${new Date().getFullYear()} ${esc(CFG.site?.copyright || CFG.site?.operator || t("app.title"))}${CFG.site?.email ? ` · ${esc(CFG.site.email)}` : ""}</p>`));
  }
  $("#rp-doc").innerHTML = pages.join("") || `<p class="muted">${t("rp.empty")}</p>`;

  // 차트: 인쇄에서도 그대로 나오도록 그린 뒤 이미지로 바꾼다
  const still = (cfg) => ({ ...cfg, options: { ...cfg.options, animation: false, responsive: false, maintainAspectRatio: false,
    plugins: { ...(cfg.options?.plugins || {}), cursorLine: false, legend: { position: "bottom", ...(cfg.options?.plugins?.legend || {}) } } } });
  if (document.getElementById("rp-weekly")) chart("rp-weekly", still({ type: "bar", data: { labels: series.items.map((s) => s.week),
    datasets: [{ label: t("kpi.outbreaks"), data: series.items.map((s) => s.outbreak), backgroundColor: "#B42318" },
               { label: t("kpi.wild"), data: series.items.map((s) => s.wild_detection), backgroundColor: "#1D4ED8" }] },
    options: { scales: { y: { beginAtZero: true, ticks: { precision: 0 } }, x: { ticks: { maxTicksLimit: 14 } } } } }));
  if (document.getElementById("rp-seasons")) {
    const n = se.series.length, sm = se.season_start_month, maxW = Math.max(0, ...se.series.map((s) => s.cum.length));
    const wk = (w) => { const d = new Date(2001, sm - 1, 1 + w * 7); return `${d.getMonth() + 1}/${d.getDate()}`; };
    chart("rp-seasons", still({ type: "line", data: { labels: Array.from({ length: maxW }, (_, w) => wk(w)), datasets: se.series.map((s, i) => ({
      label: s.label, data: s.cum, pointRadius: 0, tension: 0.15, borderWidth: i === n - 1 ? 3 : i === n - 2 ? 2 : 1.2,
      borderColor: i === n - 1 ? "#B42318" : i === n - 2 ? "#0E5A55" : `rgba(110,118,113,${0.25 + 0.5 * i / Math.max(1, n - 1)})` })) },
      options: { plugins: { legend: { labels: { boxWidth: 10, font: { size: 9 } } } },
        scales: { y: { beginAtZero: true, title: { display: true, text: t("h.cum") } }, x: { ticks: { maxTicksLimit: 13 } } } } }));
  }
  if (document.getElementById("rp-temp")) chart("rp-temp", still(tempChartCfg(A.temp)));
  if (document.getElementById("rp-birds")) chart("rp-birds", still(birdsChartCfg(A.birds)));
  document.querySelectorAll("#rp-doc canvas").forEach((c) => {
    try { const img = new Image(); img.src = c.toDataURL("image/png"); img.className = "rp-img"; c.replaceWith(img); } catch (_) { /* 그대로 둠 */ }
  });
  Object.keys(S.charts).filter((x) => x.startsWith("rp-")).forEach(clearChart);

  // 지도
  const KOREA = [[33.0, 124.8], [38.7, 130.2]];
  const mk = async (id) => { const el = document.getElementById(id); if (!el) return null;
    const m = await makeMap(el, { zoom: 7, base: "gray" }); m._el = el; RP.maps.push(m); m.fitLatLngs?.(KOREA, 7); return m; };
  const mc = await mk("rp-map-cur");
  if (mc) {
    mc.dots("prev", (evPrev?.items || []).filter((e) => e.kind === "outbreak" && e.lat != null).map((e) => ({ ...e, r: 4, color: "#9AA19C", opacity: 0.8, z: 2, title: `${e.date} ${e.place || ""}` })));
    mc.dots("cur", (evCur?.items || []).filter((e) => e.lat != null && (e.kind === "outbreak" || e.kind === "wild_detection"))
      .map((e) => ({ ...e, r: e.kind === "outbreak" ? 6 : 5, color: e.kind === "outbreak" ? "#B42318" : "#1D4ED8", opacity: 0.95, z: 5, title: `${e.date} ${e.place || ""}` })));
  }
  const mp = await mk("rp-map-pred");
  if (mp && okA) mp.dots("pred", assocPredDots(A.model.pred || []).dots);
  if (RP.maps.length) { rpStatus(t("rp.wait.tiles")); await rpSleep(3500); }
  if (token !== RP.token) return;
  RP.ready = true;
  rpStatus(t("rp.ready", { n: document.querySelectorAll("#rp-doc .rp-page").length }), false);
}

// 표지의 '핵심 내용' — 각 분석의 결론 문장을 모은다
function rpFindings(k, se, A) {
  const out = [];
  if (k.outbreaks != null) out.push(t("rp.f.kpi", { n: k.outbreaks, w: k.wild_detections ?? 0, d: k.season_start || "",
    p: k.prev ? t("rp.f.prev", { n: k.prev.outbreaks ?? 0 }) : "" }));
  if (se?.rows?.length) {
    const cur = se.rows.find((r) => r.label === se.current), past = se.rows.filter((r) => r.label !== se.current);
    if (cur && past.length) {
      const avg = past.reduce((a, r) => a + (r.by_now || 0), 0) / past.length;
      out.push(t("rp.f.season", { s: se.current, n: cur.by_now, a: avg.toFixed(1), w: (se.now_week ?? 0) + 1 }));
    }
  }
  if (A?.model?.available) {
    const v = assocModelVerdict(A.model);
    if (v.up.length) out.push(t("rp.f.model", { l: v.up.slice(0, 3).map((f) => `${rpStrip(f.label)} ×${xN(f.v)}`).join(", ") }));
    const P = A.model.pred || [];
    if (P.length) out.push(t("rp.f.pred", { l: P.slice(0, 3).map((r) => `${r.region}(×${xN(r.rel)})`).join(", ") }));
  }
  const firstVerdict = (html) => { const m = /<p class="as-verdict">([\s\S]*?)<\/p>/.exec(html || ""); return m ? rpStrip(m[1]) : ""; };
  if (A?.wild?.available) { const s = firstVerdict(assocWildHTML(A.wild)); if (s) out.push(s); }
  if (A?.temp?.available) { const s = firstVerdict(assocTempNote(A.temp)); if (s) out.push(s); }
  if (A?.visits?.available) { const s = firstVerdict(assocVisitsHTML(A.visits, { noNow: true })); if (s) out.push(s); }
  return out;
}

// PDF 저장: 파일 이름이 되도록 제목을 바꾸고 인쇄 창을 연다(대상: 'PDF로 저장')
function rpPrint() {
  if (!RP.ready) return;
  const old = document.title;
  document.title = RP.file;
  document.body.classList.add("printing-report");
  const done = () => { document.title = old; document.body.classList.remove("printing-report"); window.removeEventListener("afterprint", done); };
  window.addEventListener("afterprint", done);
  setTimeout(() => window.print(), 50);
}
