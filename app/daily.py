"""오늘의 가축질병 현황 — 매일 아침 한 장짜리 요약 대시보드(HTML 한 파일)를 만들어 GitHub Pages 로 올린다.

- build()   : 오늘 자료 묶음(가금 중심 + 돼지·소 요약). 공개 페이지라 시군 단위까지만(농장 이름·리 주소 없음)
- render()  : 외부 의존은 CDN 의 Leaflet·Chart.js 뿐인 HTML 한 파일. 지도 배경은 키가 필요 없는 CARTO(OSM)
- publish() : DAILY_PAGES_DIR(공개 저장소를 받아 둔 폴더)에 index.html 과 archive/날짜.html 을 쓰고 git push
              (git 인증은 그 맥의 설정을 그대로 씀: gh auth login 또는 자격 증명 도우미)
- 매일 DAILY_AT(기본 08:00) 이후 첫 확인 때 한 번. DAILY_PUBLISH=0 이면 끔. 서버의 /daily 에서 미리보기.
"""
from __future__ import annotations

import html
import json
import math
import os
import subprocess
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

from . import analysis as A
from . import db, news, report
from .settings import ROOT, SITE_INFO
from .species import SPECIES

OUT_DIR = ROOT / "data" / "daily"
PAGES_DIR = os.path.expanduser(os.environ.get("DAILY_PAGES_DIR", "").strip())
AT = os.environ.get("DAILY_AT", "08:00")
ENABLED = os.environ.get("DAILY_PUBLISH", "1") == "1"
state: dict = {"last": None, "error": None, "running": False}

FACTOR = {"density": "농장 수(밀집도)", "heads_per_farm": "농장당 사육두수", "duck_share": "오리 농장 비중",
          "wild_nearby": "20km 안 야생 검출(직전 2주)", "neighbor_outbreak": "30km 안 농장 발생(직전 2주)",
          "temperature": "기온(낮을수록)", "visits": "축산차량 방문"}


def _factor_text(c: dict) -> tuple[str, float, float, float]:
    """모형 계수 → 사람이 읽는 말과 위험 배수(화면의 '위험 배수'와 같은 규칙)."""
    k = lambda v: f"{math.exp(v):.1f}"  # noqa: E731
    o, lo, hi = c["or"], c["lo"], c["hi"]
    f = c["factor"]
    if f == "density":
        lab = f"농장 수 {k(c['sd'])}배 많을 때"
    elif f == "heads_per_farm":
        lab = f"농장당 사육두수 {k(c['sd'])}배 많을 때"
    elif f == "visits":
        lab = f"농장당 차량 방문 {k(c['sd'])}배 많을 때"
    elif f == "duck_share":
        lab = f"오리 농장 비중 {max(1, round(c['sd'] * 100))}%p 높을 때"
    elif f == "temperature":
        lab = f"직전 2주 기온 {c['sd']:.1f}℃ 낮을 때"
        o, lo, hi = (1 / o if o else None), (1 / c["hi"] if c["hi"] else None), (1 / c["lo"] if c["lo"] else None)
    else:
        lab = FACTOR.get(f, f) + " 있음"
    return lab, o, lo, hi


def _x(v) -> str:
    return "–" if v is None else (f"{v:,.0f}" if v >= 100 else f"{v:.1f}" if v >= 10 else f"{v:.2f}")


def _alert() -> dict | None:
    with db.session() as con:
        r = con.rows("SELECT date, note FROM event WHERE kind = 'alert' ORDER BY date DESC LIMIT 1")
    if not r:
        return None
    return {"date": str(r[0]["date"])[:10], "note": (r[0]["note"] or "").split(".")[0]}


def build(today: date | None = None) -> dict:
    today = today or date.today()
    d7 = (today - timedelta(days=7)).isoformat()
    d30 = (today - timedelta(days=30)).isoformat()
    main = report.build("poultry", None, None, assoc_wait=300)
    out = {"date": today.isoformat(), "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "alert": _alert(),
           "site": SITE_INFO, "species": {}}
    for sp in ("poultry", "pig", "cattle"):
        R = main if sp == "poultry" else report.build(sp, None, None, assoc_wait=0)
        cfg = SPECIES[sp]
        ev = R["events"]
        recent = [e for e in ev if e["date"] >= d7]
        out["species"][sp] = {
            "label": cfg["label"], "disease": cfg["diseases"].get(R["disease"], R["disease"]), "season": R["label"],
            "prev_label": R["prev_label"], "kpi": R["kpi"],
            "recent": [{"date": e["date"], "kind": e["kind"], "disease": cfg["diseases"].get(e["disease"], e["disease"]),
                        "region": " ".join(x for x in (e["sido"], e["sigungu"]) if x) or (e.get("place") or ""), "sub": e.get("subspecies") or ""}
                       for e in reversed(recent)][:12],
            "today_new": sum(1 for e in ev if e["date"] == today.isoformat() and e["kind"] == "outbreak"),
            "week_new": sum(1 for e in recent if e["kind"] == "outbreak"),
        }
    P = out["species"]["poultry"]
    # 지도: 이번 시즌 발생·야생 검출(시군 대표 좌표 그대로 — 원천 자료가 공개한 위치), 최근 30일 발생의 방역대
    P["points"] = [{"lat": round(e["lat"], 3), "lon": round(e["lon"], 3), "kind": e["kind"], "date": e["date"],
                    "region": " ".join(x for x in (e["sido"], e["sigungu"]) if x)} for e in main["events"]
                   if e["lat"] is not None and e["kind"] in ("outbreak", "wild_detection")]
    P["zones"] = [{"lat": round(e["lat"], 3), "lon": round(e["lon"], 3)} for e in main["events"]
                  if e["lat"] is not None and e["kind"] == "outbreak" and e["date"] >= d30]
    P["weekly"] = main["weekly"]
    se = main["seasons"]
    P["season_curves"] = [{"label": s["label"], "cum": s["cum"]} for s in se["series"][-6:]]
    P["season_start_month"] = se["season_start_month"]
    cur = next((r for r in se["rows"] if r["label"] == se["current"]), None)
    past = [r for r in se["rows"] if r["label"] != se["current"]]
    P["by_now"] = {"n": cur["by_now"] if cur else 0, "avg": round(sum(r["by_now"] for r in past) / len(past), 1) if past else None,
                   "week": se["now_week"] + 1}
    # 연관 분석 요약
    a = main["assoc"]
    P["model"] = None
    M = a.get("model") if isinstance(a, dict) else None
    if M and M.get("available"):
        best = M["models"][M["best"]]
        fs = []
        for c in best["coef"]:
            lab, o, lo, hi = _factor_text(c)
            fs.append({"label": lab, "or": o, "lo": lo, "hi": hi, "sig": c["sig"]})
        P["model"] = {"factors": fs, "auc": (best.get("oot") or {}).get("auc") or best.get("auc"),
                      "pred": [{"region": r["region"], "p": r["p"], "rel": r["rel"], "lat": r["lat"], "lon": r["lon"],
                                "top": FACTOR.get(r["top"], "–") if r.get("top") else "–"} for r in (M.get("pred") or [])[:15]],
                      "week": M.get("week")}
    notes = []
    W = a.get("wild") or {}
    if W.get("available"):
        cells = [dict(c, km=g["km"]) for g in W["grid"] for c in g["cells"] if c.get("sig") and (c.get("rr") or 0) > 1]
        if cells:
            b = max(cells, key=lambda c: c["lo"])
            notes.append(f"{b['km']}km 안에서 {b['lag']}일 전에 야생 검출이 있었던 시군은 농장 발생 위험이 ×{_x(b['rr'])}(오차 범위 {_x(b['lo'])}~{_x(b['hi'])}배).")
    T = a.get("temp") or {}
    if T.get("available"):
        bs = [b for b in T["bins"] if b["units"]]
        ref = next((b for b in bs if b.get("ref")), None)
        top = max((b for b in bs if b.get("sig") and (b.get("rr") or 0) > 1), key=lambda b: b["rr"], default=None)
        lab = lambda b: f"{b['tmax']}℃ 미만" if b["tmin"] <= -99 else f"{b['tmin']}℃ 이상" if b["tmax"] >= 99 else f"{b['tmin']}~{b['tmax']}℃"  # noqa: E731
        if top and ref:
            notes.append(f"직전 2주 평균기온 {lab(top)}인 주는 {lab(ref)}인 주보다 발생 위험 ×{_x(top['rr'])}.")
    V = a.get("visits") or {}
    if V.get("available"):
        q = V["dose"][-1]
        if q.get("or") and not q.get("ref"):
            notes.append(f"발생 2~4주 전 3주간 축산차량이 {q['vmin']}회 이상 드나든 리는 방문이 가장 적은 리보다 발생 위험 ×{_x(q['or'])}.")
    P["notes"] = notes
    nw = news.query(limit=10)
    out["news"] = [{"date": str(n.get("published") or n.get("fetched_at") or "")[:10], "title": n["title"],
                    "publisher": n.get("publisher") or "", "link": n.get("link") or "", "kind": n["kind"]} for n in nw["items"]]
    return out


def render(d: dict) -> str:
    data = json.dumps(d, ensure_ascii=False, default=str).replace("</", "<\\/")
    op = d["site"]
    foot = " | ".join(html.escape(x) for x in (op.get("operator"), op.get("ceo") and f"대표 {op['ceo']}", op.get("email")) if x)
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>오늘의 가축질병 현황 · {d['date']}</title>
<meta name="description" content="{d['date']} 가축질병(조류인플루엔자·아프리카돼지열병·럼피스킨 등) 발생 현황과 분석 요약">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{{--bg:#F4F5F2;--card:#fff;--ink:#17201B;--muted:#56605A;--line:#DADDD6;--soft:#EEF0EC;--teal:#0E5A55;--red:#B42318;--blue:#1D4ED8;--amber:#B54708}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121614;--card:#1B201D;--ink:#E8ECE9;--muted:#9AA59F;--line:#2E3531;--soft:#232926;--teal:#5FC3B8;--red:#F2786C;--blue:#7EA2FF;--amber:#F0A35E}}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font-family:"Noto Sans KR","Apple SD Gothic Neo",system-ui,sans-serif;font-size:14px;line-height:1.55}}
.wrap{{max-width:1280px;margin:0 auto;padding:16px}} header{{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-end;gap:8px 16px;margin:4px 0 14px}}
h1{{margin:0;font-size:22px;color:var(--teal)}} h2{{margin:0 0 8px;font-size:15px}} .muted{{color:var(--muted)}} .small{{font-size:12px}}
.alert{{background:var(--red);color:#fff;padding:8px 12px;border-radius:8px;font-weight:700;margin-bottom:12px}}
.grid{{display:grid;gap:12px;grid-template-columns:repeat(12,minmax(0,1fr))}} .card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;min-width:0}}
.c3{{grid-column:span 3}} .c4{{grid-column:span 4}} .c5{{grid-column:span 5}} .c6{{grid-column:span 6}} .c7{{grid-column:span 7}} .c8{{grid-column:span 8}} .c12{{grid-column:span 12}}
@media (max-width:900px){{.c3,.c4,.c5,.c6,.c7,.c8{{grid-column:span 12}} .kpis{{grid-template-columns:repeat(2,1fr)!important}}}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}} .kpi b{{display:block;font-size:28px;line-height:1.2}} .kpi span{{font-size:12px;color:var(--muted)}}
#map{{height:520px;border-radius:8px}} .chart{{height:240px}} table{{width:100%;border-collapse:collapse}} th,td{{text-align:left;padding:5px 6px;border-bottom:1px solid var(--soft);font-size:13px;vertical-align:top}}
th{{color:var(--muted);font-weight:500;font-size:12px}} .tag{{display:inline-block;padding:0 6px;border-radius:4px;font-size:11px;color:#fff}} .t-outbreak{{background:var(--red)}} .t-wild_detection{{background:var(--blue)}} .t-movement_restriction{{background:var(--amber)}}
.verdict{{background:var(--soft);border-radius:8px;padding:10px 12px;margin:0 0 8px}} ul{{margin:0;padding-left:18px}} li{{margin:3px 0}}
.sp{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:6px}} .sp div{{background:var(--soft);border-radius:8px;padding:8px}} .sp b{{font-size:20px;display:block}}
.bar{{height:8px;border-radius:4px;background:var(--red);display:inline-block;vertical-align:middle}} a{{color:var(--teal)}} footer{{margin:18px 0 8px;color:var(--muted);font-size:12px}}
.legend i{{display:inline-block;width:10px;height:10px;border-radius:50%;margin:0 4px 0 10px;vertical-align:-1px}}
</style></head><body><div class="wrap">
<header><div><h1>오늘의 가축질병 현황</h1><div class="muted" id="sub"></div></div><div class="muted small" id="gen"></div></header>
<div id="alert"></div>
<div class="grid">
  <div class="card c12"><h2>가금 · 고병원성 조류인플루엔자 <span class="muted small" id="season"></span></h2><div class="kpis" id="kpis"></div><p class="small muted" id="bynow" style="margin:8px 0 0"></p></div>
  <div class="card c7"><h2>이번 시즌 발생 지도</h2><div id="map"></div><div class="legend small muted" style="margin-top:6px">
    <i style="background:#B42318"></i>농장 발생 <i style="background:#1D4ED8"></i>야생 검출 <i style="background:transparent;border:2px dashed #B42318;border-radius:50%"></i>최근 30일 방역대(3·10km) <i style="background:#EDA100"></i>이번 주 위험 상위 시군</div></div>
  <div class="card c5"><h2>최근 7일 발생·검출</h2><div id="recent"></div></div>
  <div class="card c6"><h2>주간 추이 (이번 시즌)</h2><div class="chart"><canvas id="weekly"></canvas></div></div>
  <div class="card c6"><h2>시즌별 누적 발생</h2><div class="chart"><canvas id="cum"></canvas></div></div>
  <div class="card c6"><h2>이번 주 발생 위험이 높은 시군</h2><div id="pred"></div></div>
  <div class="card c6"><h2>무엇이 위험을 높이나 (과거 자료 분석)</h2><div id="factors"></div></div>
  <div class="card c6"><h2>돼지 · 소</h2><div id="others"></div></div>
  <div class="card c6"><h2>최근 뉴스·공지</h2><div id="news"></div></div>
</div>
<footer>{foot}<br>데이터 출처: 농림축산식품부(농식품 빅데이터 포털) · 공공데이터포털(행정안전부, 농림축산검역본부, 기상청) · 축산차량 방문 공개 정보 · 국립야생동물질병관리원 · 뉴스는 각 언론사.
공개 자료로 만든 참고용 요약이며 공식 방역 판단을 대신하지 않습니다. 공식 정보는 농림축산식품부와 각 지자체 발표를 확인하세요. 지도 © OpenStreetMap · CARTO.
<br><a href="archive/">지난 현황 보기</a></footer>
</div>
<script>
const D = {data};
const $ = (s) => document.querySelector(s), esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c]));
const n = (v) => v == null ? "–" : Number(v).toLocaleString("ko-KR");
const x = (v) => v == null ? "–" : v >= 100 ? Math.round(v).toLocaleString() : v >= 10 ? v.toFixed(1) : v.toFixed(2);
const KIND = {{outbreak: "농장 발생", wild_detection: "야생 검출", movement_restriction: "이동제한"}};
const P = D.species.poultry, K = P.kpi;
$("#sub").textContent = D.date + " 기준";
$("#gen").textContent = "생성 " + D.generated_at;
if (D.alert) $("#alert").innerHTML = `<div class="alert">위기경보 · ${{esc(D.alert.date)}} · ${{esc(D.alert.note)}}</div>`;
$("#season").textContent = `${{P.season}} 시즌`;
$("#kpis").innerHTML = [
  ["오늘 신규 농장 발생", P.today_new, `최근 7일 ${{P.week_new}}건`, "var(--red)"],
  ["시즌 농장 발생", K.outbreaks, `직전 ${{P.prev_label}} 같은 기간 ${{K.prev_same.outbreaks}}건`, "var(--red)"],
  ["시즌 야생 검출", K.wild_detections, `직전 시즌 같은 기간 ${{K.prev_same.wild_detections}}건`, "var(--blue)"],
  ["발생 시군", K.sigungu, K.first ? `첫 발생 ${{K.first}}` : "아직 없음", "var(--amber)"],
].map(([l, v, s, c]) => `<div class="kpi"><span>${{l}}</span><b style="color:${{c}}">${{n(v)}}</b><span>${{esc(s)}}</span></div>`).join("");
if (P.by_now.avg != null) $("#bynow").textContent = `시즌 ${{P.by_now.week}}주차까지 누적 ${{P.by_now.n}}건 — 지난 시즌들의 같은 시점 평균 ${{P.by_now.avg}}건.`;
$("#recent").innerHTML = P.recent.length ? `<table><tr><th>날짜</th><th>구분</th><th>지역</th></tr>${{P.recent.map((e) =>
  `<tr><td>${{e.date.slice(5)}}</td><td><span class="tag t-${{e.kind}}">${{KIND[e.kind] || e.kind}}</span> ${{esc(e.sub)}}</td><td>${{esc(e.region)}}</td></tr>`).join("")}}</table>` : `<p class="muted">최근 7일 동안 발생·검출이 없습니다.</p>`;
// 지도
const map = L.map("map", {{ scrollWheelZoom: false }}).setView([35.9, 127.8], 7);
L.tileLayer("https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png", {{ maxZoom: 18, attribution: "&copy; OpenStreetMap &copy; CARTO" }}).addTo(map);
P.zones.forEach((z) => {{ L.circle([z.lat, z.lon], {{ radius: 10000, color: "#B42318", weight: 1, dashArray: "4 4", fill: false }}).addTo(map);
  L.circle([z.lat, z.lon], {{ radius: 3000, color: "#B42318", weight: 1, fillOpacity: 0.08 }}).addTo(map); }});
if (P.model) P.model.pred.slice(0, 10).forEach((r) => L.circleMarker([r.lat, r.lon], {{ radius: 9, color: "#EDA100", weight: 3, fill: false }})
  .bindTooltip(`${{esc(r.region)}} · 이번 주 위험 전국 평균의 ×${{x(r.rel)}}`).addTo(map));
P.points.forEach((p) => L.circleMarker([p.lat, p.lon], {{ radius: p.kind === "outbreak" ? 5 : 4, color: "#fff", weight: 1,
  fillColor: p.kind === "outbreak" ? "#B42318" : "#1D4ED8", fillOpacity: 0.9 }}).bindTooltip(`${{p.date}} ${{KIND[p.kind]}} ${{esc(p.region)}}`).addTo(map));
// 차트
Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
new Chart($("#weekly"), {{ type: "bar", data: {{ labels: P.weekly.map((w) => w.week.slice(5)), datasets: [
  {{ label: "농장 발생", data: P.weekly.map((w) => w.outbreak), backgroundColor: "#B42318" }},
  {{ label: "야생 검출", data: P.weekly.map((w) => w.wild_detection), backgroundColor: "#1D4ED8" }}] }},
  options: {{ maintainAspectRatio: false, plugins: {{ legend: {{ position: "bottom" }} }}, scales: {{ y: {{ beginAtZero: true, ticks: {{ precision: 0 }} }} }} }} }});
const sm = P.season_start_month, L2 = P.season_curves.length, mw = Math.max(0, ...P.season_curves.map((s) => s.cum.length));
const wl = (w) => {{ const d = new Date(2001, sm - 1, 1 + w * 7); return `${{d.getMonth() + 1}}/${{d.getDate()}}`; }};
new Chart($("#cum"), {{ type: "line", data: {{ labels: Array.from({{ length: mw }}, (_, w) => wl(w)), datasets: P.season_curves.map((s, i) => ({{
  label: s.label, data: s.cum, pointRadius: 0, tension: 0.15, borderWidth: i === L2 - 1 ? 3 : 1.4,
  borderColor: i === L2 - 1 ? "#B42318" : i === L2 - 2 ? "#0E5A55" : `rgba(110,118,113,${{0.3 + 0.5 * i / Math.max(1, L2 - 1)}})` }})) }},
  options: {{ maintainAspectRatio: false, plugins: {{ legend: {{ position: "bottom", labels: {{ boxWidth: 10 }} }} }}, scales: {{ y: {{ beginAtZero: true }}, x: {{ ticks: {{ maxTicksLimit: 12 }} }} }} }} }});
// 위험 시군·요인
const M = P.model;
if (M && M.pred.length) {{
  const mx = Math.max(...M.pred.map((r) => r.rel));
  $("#pred").innerHTML = `<p class="small muted" style="margin:0 0 6px">${{esc(M.week)}} 주, 과거 자료로 만든 통계 모형의 1주 발생 확률(전국 평균 대비 배수)</p><table><tr><th>시군</th><th>평균 대비</th><th>가장 큰 요인</th></tr>${{M.pred.slice(0, 10).map((r) =>
    `<tr><td><b>${{esc(r.region)}}</b></td><td><span class="bar" style="width:${{Math.round(60 * r.rel / mx)}}px"></span> ×${{x(r.rel)}}</td><td class="muted">${{esc(r.top)}}</td></tr>`).join("")}}</table>`;
}} else $("#pred").innerHTML = `<p class="muted">예측 결과가 아직 없습니다.</p>`;
const up = M ? M.factors.filter((f) => f.sig && f.or > 1).sort((a, b) => b.or - a.or) : [];
$("#factors").innerHTML = (up.length ? `<p class="verdict">다른 조건이 같을 때 발생 위험을 뚜렷하게 높이는 요인: ${{up.map((f) => `<b>${{esc(f.label)}}</b> ×${{x(f.or)}}`).join(", ")}}</p>` : "") +
  (P.notes.length ? `<ul>${{P.notes.map((t) => `<li>${{esc(t)}}</li>`).join("")}}</ul>` : "") +
  (M && M.auc ? `<p class="small muted" style="margin:8px 0 0">모형 예측력(AUC) ${{M.auc}} · 0.5 = 찍기, 1 = 완벽. 관찰 자료의 연관이며 원인을 증명하지 않습니다.</p>` : "") || `<p class="muted">분석 결과가 아직 없습니다.</p>`;
// 돼지·소
$("#others").innerHTML = ["pig", "cattle"].map((k) => {{ const s = D.species[k], q = s.kpi;
  return `<p style="margin:0 0 2px"><b>${{esc(s.label)}}</b> · ${{esc(s.disease)}} <span class="muted small">${{esc(s.season)}}</span></p>
  <div class="sp"><div><span class="small muted">오늘 신규</span><b>${{n(s.today_new)}}</b></div><div><span class="small muted">최근 7일</span><b>${{n(s.week_new)}}</b></div><div><span class="small muted">올해 누적</span><b>${{n(q.outbreaks)}}</b></div></div>
  ${{s.recent.length ? `<p class="small" style="margin:6px 0 12px">${{s.recent.slice(0, 4).map((e) => `${{e.date.slice(5)}} ${{esc(e.disease)}} ${{esc(e.region)}}`).join(" · ")}}</p>` : `<p class="small muted" style="margin:6px 0 12px">최근 7일 발생 없음</p>`}}`; }}).join("");
$("#news").innerHTML = D.news.length ? `<ul>${{D.news.map((a) => `<li><span class="muted small">${{esc(a.date.slice(5))}} ${{esc(a.publisher)}}</span><br>${{a.link ? `<a href="${{esc(a.link)}}" target="_blank" rel="noopener">${{esc(a.title)}}</a>` : esc(a.title)}}</li>`).join("")}}</ul>` : `<p class="muted">뉴스 없음</p>`;
</script></body></html>"""


def _archive_index(dirpath: Path) -> str:
    files = sorted((p.name for p in dirpath.glob("20??-??-??.html")), reverse=True)
    rows = "".join(f'<li><a href="{f}">{f[:-5]}</a></li>' for f in files)
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>지난 가축질병 현황</title><style>body{{font-family:system-ui,sans-serif;max-width:720px;margin:24px auto;padding:0 16px}}</style></head>'
            f'<body><h1>지난 가축질병 현황</h1><p><a href="../">오늘 현황</a></p><ul>{rows}</ul></body></html>')


def generate(today: date | None = None) -> Path:
    d = build(today)
    page = render(d)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "index.html").write_text(page, encoding="utf-8")
    return OUT_DIR / "index.html"


def _git(*args) -> str:
    r = subprocess.run(["git", "-C", PAGES_DIR, *args], capture_output=True, text=True, timeout=180)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def publish(today: date | None = None) -> dict:
    """만들고, DAILY_PAGES_DIR 가 있으면 index.html·archive/날짜.html 을 커밋해 올린다."""
    today = today or date.today()
    state["running"] = True
    try:
        src = generate(today)
        res = {"date": today.isoformat(), "file": str(src), "pushed": False, "pages_dir": PAGES_DIR or None}
        if not PAGES_DIR:
            res["reason"] = ".env 에 DAILY_PAGES_DIR 가 없어(또는 서버를 다시 켜지 않아) 서버 안에만 만들었습니다"
        if PAGES_DIR:
            pd = Path(PAGES_DIR)
            if not (pd / ".git").exists():
                raise RuntimeError(f"DAILY_PAGES_DIR 가 git 저장소가 아닙니다: {PAGES_DIR}")
            try:
                _git("pull", "--ff-only", "-q")
            except RuntimeError as e:       # 빈 저장소 등은 그대로 진행
                print("[오늘 현황] pull 건너뜀:", e, flush=True)
            page = src.read_text(encoding="utf-8")
            (pd / "archive").mkdir(exist_ok=True)
            (pd / "index.html").write_text(page, encoding="utf-8")
            (pd / "archive" / f"{today.isoformat()}.html").write_text(page.replace('href="archive/"', 'href="./"'), encoding="utf-8")
            (pd / "archive" / "index.html").write_text(_archive_index(pd / "archive"), encoding="utf-8")
            (pd / ".nojekyll").write_text("", encoding="utf-8")
            _git("add", "-A")
            if _git("status", "--porcelain"):
                _git("commit", "-q", "-m", f"오늘의 가축질병 현황 {today.isoformat()}")
                _git("push", "-q")
                res["pushed"] = True
            else:
                res["reason"] = "올린 내용과 바뀐 것이 없습니다"
        state["last"] = {**res, "at": datetime.now().strftime("%Y-%m-%d %H:%M")}
        state["error"] = None
        print(f"[오늘 현황] {res}", flush=True)
        return res
    except Exception as e:
        state["error"] = str(e)[:400]
        print(f"[오늘 현황] 실패: {e}", flush=True)
        raise
    finally:
        state["running"] = False


def _done_today(today: date) -> bool:
    last = state.get("last")
    if last and last.get("date") == today.isoformat():
        return True
    f = Path(PAGES_DIR) / "archive" / f"{today.isoformat()}.html" if PAGES_DIR else OUT_DIR / f"done-{today.isoformat()}"
    return f.exists()


def _loop() -> None:
    time.sleep(90)
    hh, mm = (int(v) for v in AT.split(":"))
    while True:
        try:
            now = datetime.now()
            if now.hour * 60 + now.minute >= hh * 60 + mm and not _done_today(now.date()):
                publish(now.date())
                if not PAGES_DIR:
                    (OUT_DIR / f"done-{now.date().isoformat()}").write_text("", encoding="utf-8")
        except Exception:
            traceback.print_exc()
        time.sleep(300)


def start() -> None:
    if ENABLED and not state.get("thread"):
        state["thread"] = True
        threading.Thread(target=_loop, name="daily-page", daemon=True).start()


def status() -> dict:
    return {**{k: v for k, v in state.items() if k != "thread"}, "pages_dir": PAGES_DIR or None, "at": AT, "enabled": ENABLED}
