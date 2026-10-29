"""연관 분석 — 요인이 있을 때 발생이 얼마나 늘어나는지(상대위험도·오즈비)를 계산한다.

분석 단위는 '시군 × 주'(주는 월요일 시작). 각 주의 요인은 모두 그 주 **이전** 자료로만 만든다(미래 정보 누설 없음).
발생이 사실상 없는 달(전체 발생의 2% 미만인 달)은 빼서 '여름엔 춥지 않고 발생도 없다' 같은 계절 효과가 섞이는 것을 줄인다.

  6 model   : 로지스틱 회귀(요인을 함께 넣음) → 요인별 오즈비 + 이번 주 시군별 예측 확률
  1 wild    : 야생 검출 후 반경 10/20/30km × 시차 1~4주 → 농장 발생 상대위험도
  2 temp    : 직전 14일 평균기온 구간별 발생률·상대위험도
  3 visits  : 리 단위 직전 3주 축산차량 방문량 5분위별 오즈비(사례-코호트), 차량유형별 오즈비
  4 density : 시군 농장 수·농장당 사육두수·오리 농장 비중 5분위별 상대위험도
  5 birds   : 연도별 철새 북상 시기와 시즌 발생 종료 시점

관찰 자료이므로 연관일 뿐 원인을 증명하지 않는다. 계산은 별도 스레드에서 하고 결과를 기억해 둔다
(데이터가 바뀌면 다시 계산, 그동안은 이전 결과를 보여 준다).
"""
from __future__ import annotations

import bisect
import math
import re
import threading
import time
import traceback
from collections import Counter, defaultdict
from datetime import date, timedelta
from statistics import median

from . import analysis as A
from . import db, geo
from .species import SPECIES
from .stations import ASOS

MAX_SEASONS = 12
LAGS = [(1, 7), (8, 14), (15, 21), (22, 28)]
RADII = [10, 20, 30]
TEMP_BINS = [(-99, -5), (-5, 0), (0, 5), (5, 10), (10, 15), (15, 99)]
EXPO_WEEKS = (2, 3, 4)            # 차량 방문 노출: 발생 주의 2~4주 전(바로 앞 1주 제외)
RESPONSE = re.compile(r"시료|방역|사체|살처분|매몰")   # 발생 뒤 대응으로 드나드는 차량(원인이 아니라 결과일 수 있음)
MIN_POS = 15                       # 모형에 넣으려면 발생 주가 최소 이만큼 있어야 함
EPV = 10                           # 모형 요인 하나당 필요한 발생 주 수
MIN_UNITS = 30                     # 상대위험도를 내려면 노출 단위(시군·주)가 최소 이만큼

_cache: dict = {}
_jobs: dict = {}
_lock = threading.Lock()


# ── 공통 ────────────────────────────────────────────────────────────────
SIDO_SHORT = {"경상남도": "경남", "경상북도": "경북", "전라남도": "전남", "전라북도": "전북", "전북특별자치도": "전북",
              "충청남도": "충남", "충청북도": "충북", "강원도": "강원", "강원특별자치도": "강원", "제주특별자치도": "제주",
              "경기도": "경기", "세종특별자치시": "세종"}


def _sido(s: str | None) -> str:
    s = (s or "").strip()
    if s.startswith("전남광주"):          # 2026 통합특별시: 시군은 옛 전남, 구는 옛 광주
        return "전남"
    return SIDO_SHORT.get(s, s[:2])


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _rr(a: int, n1: int, c: int, n0: int) -> dict:
    """노출군 a/n1, 비노출군 c/n0 → 상대위험도와 95% 신뢰구간(로그 정규근사)."""
    out = {"a": a, "n1": n1, "c": c, "n0": n0,
           "rate1": round(a / n1 * 1000, 2) if n1 else None, "rate0": round(c / n0 * 1000, 2) if n0 else None}
    if not (n1 and n0) or c == 0 or n1 < MIN_UNITS:
        return {**out, "rr": None, "lo": None, "hi": None, "few": bool(n1) and n1 < MIN_UNITS}
    if a == 0:                            # 노출군 발생 0: 점추정 0, 상한만 0.5 보정으로
        hi = ((0.5 / (n1 + 1)) / ((c + 0.5) / (n0 + 1))) * math.exp(1.96 * math.sqrt(2 - 1 / (n1 + 1) + 1 / (c + 0.5) - 1 / (n0 + 1)))
        return {**out, "rr": 0.0, "lo": 0.0, "hi": round(hi, 2), "sig": hi < 1}
    rr = (a / n1) / (c / n0)
    se = math.sqrt(max(1 / a - 1 / n1 + 1 / c - 1 / n0, 0))
    lo, hi = rr * math.exp(-1.96 * se), rr * math.exp(1.96 * se)
    return {**out, "rr": round(rr, 2), "lo": round(lo, 2), "hi": round(hi, 2), "sig": lo > 1 or hi < 1}


def _or(a: int, b: int, c: int, d: int) -> dict:
    """사례 a(노출)/c(비노출), 대조 b(노출)/d(비노출) → 오즈비와 95% 신뢰구간."""
    out = {"cases1": a, "ctrl1": b, "cases0": c, "ctrl0": d}
    if min(b, d) == 0 or (a + c) == 0:
        return {**out, "or": None, "lo": None, "hi": None}
    a2, b2, c2, d2 = (x + 0.5 for x in (a, b, c, d)) if 0 in (a, b, c, d) else (a, b, c, d)
    o = a2 * d2 / (b2 * c2)
    se = math.sqrt(1 / a2 + 1 / b2 + 1 / c2 + 1 / d2)
    lo, hi = o * math.exp(-1.96 * se), o * math.exp(1.96 * se)
    return {**out, "or": round(o, 2), "lo": round(lo, 2), "hi": round(hi, 2), "sig": lo > 1 or hi < 1}


def _quantiles(vals: list[float], k: int = 5) -> list[float]:
    s = sorted(vals)
    return [s[min(len(s) - 1, int(len(s) * i / k))] for i in range(1, k)] if s else []


def _qbin(v: float, cuts: list[float]) -> int:
    return bisect.bisect_right(cuts, v)


# ── 패널 만들기 ─────────────────────────────────────────────────────────
def _regions(species: str) -> tuple[dict, bool]:
    """시군 목록과 대표점·농장 수(농림부 읍면동 통계를 시군으로 묶음). 통계가 없으면 발생 지역으로 대신."""
    regs: dict[str, dict] = {}
    fd = A.farm_density(species, "sigungu")
    for r in fd["items"]:
        k = f"{_sido(r['sido'])} {r['sigungu']}"
        g = regs.setdefault(k, {"key": k, "sido": r["sido"], "sigungu": r["sigungu"], "farms": 0, "heads": None,
                                "lat": r["lat"], "lon": r["lon"]})
        g["farms"] += r["farms"] or 0
        if r.get("heads") is not None:
            g["heads"] = (g["heads"] or 0) + r["heads"]
        if g["lat"] is None and r["lat"] is not None:
            g["lat"], g["lon"] = r["lat"], r["lon"]
    with db.session() as con:
        ev = con.rows("SELECT sido, sigungu, lat, lon FROM event WHERE species = ? AND kind = 'outbreak' "
                      "AND sigungu IS NOT NULL AND sigungu <> ''", [species])
        duck = con.rows("SELECT sido, sigungu, SUM(value) AS n FROM region_stat WHERE source = ? AND species = ? "
                        "AND metric LIKE ? AND sigungu <> '' GROUP BY sido, sigungu", [A.FARM_SOURCE, species, "farms%오리%"]) \
            if species == "poultry" else []
    have_farms = bool(regs)
    pts = defaultdict(list)
    for e in ev:
        k = f"{_sido(e['sido'])} {e['sigungu']}"
        if e["lat"] is not None:
            pts[k].append((e["lat"], e["lon"]))
        if not have_farms and k not in regs:
            regs[k] = {"key": k, "sido": e["sido"], "sigungu": e["sigungu"], "farms": None, "heads": None, "lat": None, "lon": None}
    for k, g in regs.items():
        if g["lat"] is None and pts.get(k):
            g["lat"] = sum(p[0] for p in pts[k]) / len(pts[k])
            g["lon"] = sum(p[1] for p in pts[k]) / len(pts[k])
    for d in duck:
        k = f"{_sido(d['sido'])} {d['sigungu']}"
        if k in regs and regs[k]["farms"]:
            regs[k]["duck"] = min(1.0, (regs[k].get("duck_n", 0) + (d["n"] or 0)) / regs[k]["farms"])
            regs[k]["duck_n"] = regs[k].get("duck_n", 0) + (d["n"] or 0)
    regs = {k: g for k, g in regs.items() if g["lat"] is not None and (g["farms"] is None or g["farms"] > 0)}
    for g in regs.values():
        g.setdefault("duck", 0.0 if species == "poultry" and have_farms else None)
        g["hpf"] = g["heads"] / g["farms"] if g.get("heads") and g.get("farms") else None
    return regs, have_farms


class _Matcher:
    """(시도, 시군, 좌표) → 시군 키. 시군 이름이 하나뿐이면 그것, 겹치면 시도로, 그래도 안 되면 가까운 대표점(25km 이내)."""

    def __init__(self, regs: dict):
        self.regs = regs
        self.by = defaultdict(list)
        for k, g in regs.items():
            self.by[g["sigungu"]].append(k)

    def __call__(self, sido, sigungu, lat=None, lon=None):
        c = self.by.get((sigungu or "").strip(), [])
        if len(c) == 1:
            return c[0]
        s = _sido(sido)
        for k in c:
            if k.startswith(s + " "):
                return k
        if lat is not None:
            best = min(self.regs.values(), key=lambda g: geo.haversine_km(lat, lon, g["lat"], g["lon"]), default=None)
            if best and geo.haversine_km(lat, lon, best["lat"], best["lon"]) <= 25:
                return best["key"]
        return None


def _panel(species: str, disease: str | None) -> dict:
    cfg = SPECIES[species]
    regs, have_farms = _regions(species)
    match = _Matcher(regs)
    dw, dp = A._disease_filter(disease)
    with db.session() as con:
        obs = con.rows(f"SELECT date, sido, sigungu, place, lat, lon FROM event WHERE species = ? AND kind = 'outbreak' AND {dw} "
                       f"ORDER BY date", [species] + dp)
        wild = con.rows(f"SELECT date, lat, lon FROM event WHERE species = ? AND kind = 'wild_detection' AND {dw} "
                        f"AND lat IS NOT NULL ORDER BY date", [cfg.get("wild") or "-"] + dp) if cfg.get("wild") else []
    for e in obs:
        e["d"] = A._d(str(e["date"]))
        e["key"] = match(e["sido"], e["sigungu"], e["lat"], e["lon"])
    for w in wild:
        w["d"] = A._d(str(w["date"]))
    today = date.today()
    sm = cfg["season_start_month"]
    if not obs:
        return {"regs": regs, "units": [], "obs": obs, "wild": wild, "have_farms": have_farms, "months": [], "weeks": []}
    # 발생이 있는 달(전체의 2% 이상)만 분석
    mc = Counter(e["d"].month for e in obs)
    months = sorted(m for m, n in mc.items() if n / len(obs) >= 0.02)
    last_season = A.season_of(today, sm)
    first_season = max(A.season_of(obs[0]["d"], sm), last_season - MAX_SEASONS + 1)
    start = _monday(A._d(A.season_bounds(first_season, sm)[0]))
    end = _monday(today)                                     # 이번 주(예측용)는 마지막에 따로
    weeks = []
    w = start
    while w < end:
        if w.month in months or (w + timedelta(days=6)).month in months:
            weeks.append(w)
        w += timedelta(days=7)
    # 시군별: 30km 안 야생 검출·발생의 (날짜, 거리) — 날짜순
    near_w: dict = {}
    near_o: dict = {}
    for k, g in regs.items():
        near_w[k] = sorted((x["d"], geo.haversine_km(g["lat"], g["lon"], x["lat"], x["lon"])) for x in wild
                           if abs(x["lat"] - g["lat"]) < 0.3 and abs(x["lon"] - g["lon"]) < 0.4)
        near_w[k] = [p for p in near_w[k] if p[1] <= 30]
        near_o[k] = sorted(x["d"] for x in obs if x["lat"] is not None
                           and abs(x["lat"] - g["lat"]) < 0.3 and abs(x["lon"] - g["lon"]) < 0.4
                           and geo.haversine_km(g["lat"], g["lon"], x["lat"], x["lon"]) <= 30)
    cases = Counter((e["key"], _monday(e["d"])) for e in obs if e["key"])
    temp = _temps(regs, weeks + [end])
    visits = _region_visits(species, regs, match, weeks + [end])
    units = []
    for k in regs:
        nw, no = near_w[k], near_o[k]
        nwd = [p[0] for p in nw]
        for ws in weeks + [end]:
            u = {"k": k, "w": ws, "y": 1 if cases.get((k, ws)) else 0, "n": cases.get((k, ws), 0)}
            for r in RADII:
                for lo, hi in LAGS:
                    a, b = ws - timedelta(days=hi), ws - timedelta(days=lo)
                    i = bisect.bisect_left(nwd, a)
                    hit = False
                    while i < len(nw) and nw[i][0] <= b:
                        if nw[i][1] <= r:
                            hit = True
                            break
                        i += 1
                    u[f"w{r}_{lo}"] = hit
            u["wild14"] = u["w20_1"] or u["w20_8"]
            i = bisect.bisect_left(no, ws - timedelta(days=14))
            u["neigh"] = i < len(no) and no[i] < ws
            u["temp"] = temp.get((k, ws))
            u["visits"] = visits.get((k, ws))
            units.append(u)
    return {"regs": regs, "units": units, "obs": obs, "wild": wild, "have_farms": have_farms, "months": months,
            "weeks": weeks, "current": end, "temp_cov": _cov([u for u in units if u["w"] < end], "temp"),
            "visit_cov": _cov([u for u in units if u["w"] < end], "visits")}


def _cov(units, f):
    us = [u for u in units if u[f] is not None]
    if not us:
        return None
    return {"from": min(u["w"] for u in us).isoformat(), "to": max(u["w"] for u in us).isoformat(),
            "units": len(us), "cases": sum(u["y"] for u in us)}


def _temps(regs: dict, weeks: list[date]) -> dict:
    """시군 → 가장 가까운 ASOS 관측소의 직전 14일 평균기온(7일 이상 자료가 있을 때만)."""
    with db.session() as con:
        rows = con.rows("SELECT place, date, value FROM context_ts WHERE source = 'kma_asos_daily' AND metric = 'temp_avg' "
                        "AND value IS NOT NULL")
    by: dict = defaultdict(dict)
    for r in rows:
        sid = str(r["place"]).split(":")[0]
        if sid in ASOS:
            by[sid][A._d(str(r["date"]))] = r["value"]
    if not by:
        return {}
    stn = {k: min(by, key=lambda s: geo.haversine_km(g["lat"], g["lon"], ASOS[s][1], ASOS[s][2])) for k, g in regs.items()}
    cache: dict = {}
    out = {}
    for k in regs:
        s = stn[k]
        for ws in weeks:
            ck = (s, ws)
            if ck not in cache:
                vals = [by[s][d] for d in (ws - timedelta(days=i) for i in range(1, 15)) if d in by[s]]
                cache[ck] = round(sum(vals) / len(vals), 2) if len(vals) >= 7 else None
            if cache[ck] is not None:
                out[(k, ws)] = cache[ck]
    return out


def _week_expr(col: str) -> str:
    return f"CAST(date_trunc('week', {col}) AS DATE)" if db.IS_PG else f"date({col}, 'weekday 0', '-6 days')"


def _covered_weeks() -> set:
    """방문 자료가 5일 이상 있는 주(월요일)."""
    with db.session() as con:
        days = [A._d(str(r["date"])) for r in con.rows("SELECT date FROM vehicle_visit_day")]
    c = Counter(_monday(d) for d in days)
    return {w for w, n in c.items() if n >= 5}


def _region_visits(species: str, regs: dict, match, weeks: list[date]) -> dict:
    """시군 단위 직전 3주 축산차량 방문(농장 1곳당, log1p). 직전 3주가 모두 수집된 주만."""
    cov = _covered_weeks()
    if not cov:
        return {}
    kc = A._kind_cond(A._vkind_ids(species))
    with db.session() as con:
        rows = con.rows(f"SELECT p.sido, p.sigungu, {_week_expr('v.date')} AS wk, SUM(v.n) AS n FROM vehicle_visit v "
                        f"JOIN visit_place p ON p.id = v.place_id WHERE 1=1{kc} GROUP BY p.sido, p.sigungu, {_week_expr('v.date')}")
    wk: dict = defaultdict(float)
    for r in rows:
        k = match(r["sido"], r["sigungu"])
        if k:
            wk[(k, A._d(str(r["wk"])))] += r["n"] or 0
    out = {}
    for k, g in regs.items():
        f = g.get("farms") or 1
        for ws in weeks:
            prev = [ws - timedelta(days=7 * i) for i in (1, 2, 3)]
            if all(p in cov for p in prev):
                out[(k, ws)] = math.log1p(sum(wk.get((k, p), 0) for p in prev) / f)
    return out


# ── 1. 야생 검출 → 농장 발생 ─────────────────────────────────────────────
def wild_lag(P: dict) -> dict:
    us = [u for u in P["units"] if u["w"] < P["current"]]
    if not P["wild"]:
        return {"available": False, "reason": "no_wild"}
    if len(P["wild"]) < 20:
        return {"available": False, "reason": "few_wild", "wild_events": len(P["wild"])}
    grid = []
    for r in RADII:
        row = []
        for lo, hi in LAGS:
            f = f"w{r}_{lo}"
            a = sum(u["y"] for u in us if u[f]); n1 = sum(1 for u in us if u[f])
            c = sum(u["y"] for u in us if not u[f]); n0 = len(us) - n1
            row.append({"lag": f"{lo}-{hi}", **_rr(a, n1, c, n0)})
        grid.append({"km": r, "cells": row})
    return {"available": True, "grid": grid, "units": len(us), "cases": sum(u["y"] for u in us),
            "wild_events": len(P["wild"])}


# ── 2. 기온 ─────────────────────────────────────────────────────────────
def temperature(P: dict) -> dict:
    us = [u for u in P["units"] if u["w"] < P["current"] and u["temp"] is not None]
    if not us or sum(u["y"] for u in us) < 5:
        from . import weather
        return {"available": False, "reason": "no_weather", "coverage": P.get("temp_cov"), "backfill": weather.status()}
    bins = []
    for lo, hi in TEMP_BINS:
        g = [u for u in us if lo <= u["temp"] < hi]
        bins.append({"tmin": lo, "tmax": hi, "units": len(g), "cases": sum(u["y"] for u in g)})
    ref = max((b for b in bins if b["units"] >= 50), key=lambda b: b["tmin"], default=None)
    for b in bins:
        b.update(_rr(b["cases"], b["units"], ref["cases"], ref["units"]) if ref and b is not ref and b["units"] else
                 {"rr": 1.0 if b is ref else None, "rate1": round(b["cases"] / b["units"] * 1000, 2) if b["units"] else None})
        b["ref"] = b is ref
    return {"available": True, "bins": bins, "coverage": P["temp_cov"]}


# ── 3. 축산차량 방문(리 단위) ─────────────────────────────────────────────
def visit_dose(species: str, P: dict) -> dict:
    cov = _covered_weeks()
    if not cov:
        return {"available": False, "reason": "no_visits"}
    kid = A._vkind_ids(species)
    kc = A._kind_cond(kid)
    with db.session() as con:
        places = {r["address"]: r["id"] for r in con.rows("SELECT id, address FROM visit_place")}
        kinds = {r["id"]: r["vehicle"] for r in con.rows("SELECT id, vehicle FROM visit_kind")}
        nplace = len(places)
        case_pid = {}
        for e in P["obs"]:
            pid = places.get(e["place"] or "")
            if pid and _monday(e["d"]) in cov:
                case_pid[(pid, _monday(e["d"]))] = 1
        k = max(1, nplace // 2000)                 # 대조군: 리 약 2,000곳 표본(id 나머지로 고정 추출)
        ids = sorted({p for p, _ in case_pid})
        cond = f"(v.place_id % {k} = 0" + (f" OR v.place_id IN ({','.join(str(i) for i in ids)}))" if ids else ")")
        rows = con.rows(f"SELECT v.place_id, {_week_expr('v.date')} AS wk, v.kind_id, SUM(v.n) AS n FROM vehicle_visit v "
                        f"WHERE {cond}{kc} GROUP BY v.place_id, {_week_expr('v.date')}, v.kind_id")
    wk: dict = defaultdict(lambda: defaultdict(float))       # (place, week) → {vehicle: n}
    for r in rows:
        wk[(r["place_id"], A._d(str(r["wk"])))][kinds.get(r["kind_id"], "?")] += r["n"] or 0
    months = set(P["months"])
    # 노출 = 발생 주의 2~4주 전 방문(바로 앞 1주는 뺀다: 의심 신고 뒤 시료채취·방역 차량이 드나드는 기간이라 결과가 원인처럼 보임)
    weeks = sorted(w for w in cov if w.month in months and all(w - timedelta(days=7 * i) in cov for i in EXPO_WEEKS))
    if not weeks:
        return {"available": False, "reason": "short_coverage"}
    sample = {p for p, _ in wk if p % k == 0}

    def expo(pid, w):
        tot: Counter = Counter()
        for i in EXPO_WEEKS:
            for v, n in wk.get((pid, w - timedelta(days=7 * i)), {}).items():
                tot[v] += n
        return tot
    wset = set(weeks)
    cases = [expo(p, w) for (p, w) in case_pid if w in wset]
    ctrls = [expo(p, w) for p in sample for w in weeks if (p, w) not in case_pid]
    if len(cases) < 5 or not ctrls:
        return {"available": False, "reason": "few_cases", "cases": len(cases)}
    tot = lambda c: sum(n for v, n in c.items() if not RESPONSE.search(v))  # noqa: E731  대응 차량은 합계에서 뺀다
    # 구간: 방문 없음 + 방문이 있던 리·주를 4등분(같은 값이 많아도 빈 구간이 생기지 않게 경계를 겹치지 않게)
    pos = sorted(tot(c) for c in ctrls if tot(c) > 0)
    cuts = sorted({pos[min(len(pos) - 1, int(len(pos) * i / 4))] for i in (1, 2, 3)}) if pos else []
    binf = lambda v: 0 if v <= 0 else 1 + bisect.bisect_left(cuts, v)  # noqa: E731
    nb = 2 + len(cuts)
    qc = Counter(binf(tot(c)) for c in cases)
    qk = Counter(binf(tot(c)) for c in ctrls)
    ref = [0] if qc[0] >= 3 else [0, 1]          # 방문 없음 구간에 발생이 너무 적으면 다음 구간과 합쳐 기준으로
    rc, rk = sum(qc[i] for i in ref), sum(qk[i] for i in ref)
    dose = []
    bounds = [0, 0] + cuts
    for q in range(nb):
        lo = 0 if q == 0 else int(bounds[q]) + 1 if q > 1 else 1
        hi = 0 if q == 0 else (int(cuts[q - 1]) if q - 1 < len(cuts) else None)
        o = {"or": 1.0} if q in ref else _or(qc[q], qk[q], rc, rk)
        dose.append({"q": q, "vmin": lo, "vmax": hi, "ref": q in ref, "cases": qc[q], "ctrls": qk[q],
                     **{x: o.get(x) for x in ("or", "lo", "hi", "sig")}})
    vk = []
    for v in sorted({v for c in ctrls for v in c} | {v for c in cases for v in c}):
        cv = sorted(c.get(v, 0) for c in ctrls)
        thr = cv[int(len(cv) * 0.75)]
        a_ = sum(1 for c in cases if c.get(v, 0) > thr); cc = len(cases) - a_
        b_ = sum(1 for c in ctrls if c.get(v, 0) > thr); d_ = len(ctrls) - b_
        if a_ + b_ < 10:                      # 그 유형이 거의 안 다니면 건너뜀
            continue
        vk.append({"vehicle": v, "threshold": round(thr), "response": bool(RESPONSE.search(v)), **_or(a_, b_, cc, d_)})
    vk.sort(key=lambda x: (x["response"], -(x["or"] or 0)))
    with db.session() as con:
        last = con.scalar("SELECT MAX(date) FROM vehicle_visit_day")
        now_rows = []
        if last:
            d1 = A._d(str(last))
            d0 = d1 - timedelta(days=20)
            top = con.rows(f"SELECT v.place_id, SUM(v.n) AS n FROM vehicle_visit v WHERE v.date BETWEEN ? AND ?{kc} "
                           f"GROUP BY v.place_id ORDER BY SUM(v.n) DESC LIMIT 15", [d0, d1])
            if top:
                ids = [r["place_id"] for r in top]
                addr = {r["id"]: r["address"] for r in con.rows(
                    f"SELECT id, address FROM visit_place WHERE id IN ({','.join('?' * len(ids))})", ids)}
                kinds_now = defaultdict(Counter)
                for r in con.rows(f"SELECT v.place_id, v.kind_id, SUM(v.n) AS n FROM vehicle_visit v WHERE v.date BETWEEN ? AND ? "
                                  f"AND v.place_id IN ({','.join('?' * len(ids))}){kc} GROUP BY v.place_id, v.kind_id", [d0, d1] + ids):
                    kinds_now[r["place_id"]][kinds.get(r["kind_id"], "?")] += r["n"]
                now_rows = [{"address": addr.get(r["place_id"], "?"), "visits": r["n"], "group": binf(r["n"] * len(EXPO_WEEKS) / 3),
                             "top": [v for v, _ in kinds_now[r["place_id"]].most_common(3)]} for r in top]
    now = {"from": d0.isoformat(), "to": d1.isoformat(), "rows": now_rows} if last else None
    return {"available": True, "dose": dose, "by_vehicle": vk, "cases": len(cases), "ctrls": len(ctrls), "now": now, "nbins": nb,
            "sample_every": k, "coverage": {"from": weeks[0].isoformat(), "to": weeks[-1].isoformat(), "weeks": len(weeks)}}


# ── 4. 농장 밀집도·규모·오리 비중 ─────────────────────────────────────────
def density(P: dict) -> dict:
    if not P["have_farms"]:
        return {"available": False, "reason": "no_farms"}
    us = [u for u in P["units"] if u["w"] < P["current"]]
    regs = P["regs"]
    out = []
    for f, lab in (("farms", "farms"), ("hpf", "heads_per_farm"), ("duck", "duck_share")):
        vals = {k: g.get(f) for k, g in regs.items() if g.get(f) is not None}
        if len(vals) < 10 or len(set(vals.values())) < 3:
            continue
        cuts = _quantiles(list(vals.values()))
        q = {k: _qbin(v, cuts) for k, v in vals.items()}
        cnt = [[0, 0] for _ in range(5)]
        for u in us:
            if u["k"] in q:
                cnt[q[u["k"]]][0] += u["y"]; cnt[q[u["k"]]][1] += 1
        rows = []
        for i in range(5):
            r = _rr(cnt[i][0], cnt[i][1], cnt[0][0], cnt[0][1]) if i else \
                {"rr": 1.0, "rate1": round(cnt[0][0] / cnt[0][1] * 1000, 2) if cnt[0][1] else None}
            vs = [v for kk, v in vals.items() if q[kk] == i]
            rows.append({"q": i + 1, "vmin": round(min(vs), 3) if vs else None, "vmax": round(max(vs), 3) if vs else None,
                         "regions": len(vs), "cases": cnt[i][0], "units": cnt[i][1],
                         **{x: r.get(x) for x in ("rr", "lo", "hi", "rate1", "sig")}})
        hiq = max(range(1, 5), key=lambda i: rows[i]["rr"] or 0)
        names = sorted((k for k in q if q[k] == hiq), key=lambda k: -vals[k])
        out.append({"factor": lab, "rows": rows, "top_q": hiq + 1, "top_regions": names[:15], "top_n": len(names)})
    return {"available": bool(out), "factors": out, "regions": len(regs)}


# ── 5. 철새 북상 시기 ─────────────────────────────────────────────────────
def birds_timing(species: str, P: dict) -> dict:
    if species != "poultry":
        return {"available": False, "reason": "not_poultry"}
    with db.session() as con:
        bt = con.rows("SELECT year, depart, dest_country FROM bird_track WHERE depart IS NOT NULL")
    if not bt:
        return {"available": False, "reason": "no_birds"}
    sm = SPECIES[species]["season_start_month"]
    by_season = defaultdict(list)
    for e in P["obs"]:
        by_season[A.season_of(e["d"], sm)].append(e["d"])
    dep = defaultdict(list)
    for b in bt:
        d = A._d(str(b["depart"]))
        dep[A.season_of(d, sm)].append(d)
    rows = []
    for s in sorted(dep):
        ds = sorted(dep[s])
        md = ds[len(ds) // 2]
        ob = sorted(by_season.get(s, []))
        p90 = ob[min(len(ob) - 1, int(len(ob) * 0.9))] if ob else None
        rows.append({"season": A.season_label(s, sm), "tracks": len(ds), "depart_first": ds[0].isoformat(),
                     "depart_median": md.isoformat(), "outbreaks": len(ob),
                     "last": ob[-1].isoformat() if ob else None, "p90": p90.isoformat() if p90 else None,
                     "after_median": sum(1 for d in ob if d > md),
                     "lag_p90": (p90 - md).days if p90 else None, "lag_last": (ob[-1] - md).days if ob else None})
    # 1~5월 주차별(1월 1일 기준) 발생·출발 수 — 연도 합산
    wk_ob, wk_dep = Counter(), Counter()
    for s, ds in by_season.items():
        for d in ds:
            if 1 <= d.month <= 5:
                wk_ob[(d - date(d.year, 1, 1)).days // 7] += 1
    for ds in dep.values():
        for d in ds:
            if 1 <= d.month <= 5:
                wk_dep[(d - date(d.year, 1, 1)).days // 7] += 1
    lags = [r["lag_p90"] for r in rows if r["lag_p90"] is not None and r["outbreaks"] >= 3]
    return {"available": True, "rows": rows, "weeks": list(range(22)),
            "outbreaks": [wk_ob[i] for i in range(22)], "departs": [wk_dep[i] for i in range(22)],
            "lag_median": median(lags) if lags else None, "lag_range": [min(lags), max(lags)] if lags else None}


# ── 6. 종합 모형(로지스틱 회귀) ───────────────────────────────────────────
def _sig(x: float) -> float:
    return 1 / (1 + math.exp(-x)) if x > -35 else 1e-15


def _solve(M: list[list[float]], v: list[float]) -> list[float]:
    n = len(v)
    a = [row[:] + [v[i]] for i, row in enumerate(M)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(a[r][c]))
        a[c], a[p] = a[p], a[c]
        if abs(a[c][c]) < 1e-12:
            a[c][c] = 1e-12
        for r in range(n):
            if r != c:
                f = a[r][c] / a[c][c]
                if f:
                    for j in range(c, n + 1):
                        a[r][j] -= f * a[c][j]
    return [a[i][n] / a[i][i] for i in range(n)]


def _inv(M: list[list[float]]) -> list[list[float]]:
    n = len(M)
    cols = [_solve(M, [1.0 if i == j else 0.0 for i in range(n)]) for j in range(n)]
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def _pll(X, y, b, ridge):
    """벌점 로그우도(클수록 좋음). log(1+e^η)는 넘침 없이 계산."""
    ll = 0.0
    for xi, yi in zip(X, y):
        eta = sum(bj * xj for bj, xj in zip(b, xi))
        ll += yi * eta - (eta + math.log1p(math.exp(-eta)) if eta > 0 else math.log1p(math.exp(eta)))
    return ll - ridge / 2 * sum(bj * bj for bj in b[1:])


def logit_fit(X: list[list[float]], y: list[int], ridge: float = 1.0, iters: int = 40) -> dict:
    """뉴턴-랩슨 로지스틱 회귀(절편 제외 계수에 약한 L2 — 완전분리 방지). X 첫 열은 1(절편).

    한 걸음이 벌점 로그우도를 낮추면 걸음을 반씩 줄인다(발생이 드문 자료에서 계수가 발산하는 것을 막음).
    """
    p = len(X[0])
    b = [0.0] * p
    ybar = sum(y) / len(y)
    b[0] = math.log(max(ybar, 1e-6) / max(1 - ybar, 1e-6))
    ll = _pll(X, y, b, ridge)
    H = [[1.0 if i == j else 0.0 for j in range(p)] for i in range(p)]
    for _ in range(iters):
        H = [[0.0] * p for _ in range(p)]
        g = [0.0] * p
        for xi, yi in zip(X, y):
            eta = sum(bj * xj for bj, xj in zip(b, xi))
            mu = _sig(eta)
            w = mu * (1 - mu)
            r = yi - mu
            for j in range(p):
                xj = xi[j]
                if xj == 0:
                    continue
                g[j] += r * xj
                wx = w * xj
                Hj = H[j]
                for k2 in range(j, p):
                    Hj[k2] += wx * xi[k2]
        for j in range(p):
            for k2 in range(j):
                H[j][k2] = H[k2][j]
            if j:
                H[j][j] += ridge
                g[j] -= ridge * b[j]
        step = _solve(H, g)
        mx = max(abs(s) for s in step)
        if mx > 3:                                   # 한 번에 너무 멀리 가지 않게
            step = [s * 3 / mx for s in step]
        t = 1.0
        for _h in range(30):
            nb = [bj + t * sj for bj, sj in zip(b, step)]
            nll = _pll(X, y, nb, ridge)
            if nll >= ll - 1e-9:
                break
            t /= 2
        else:
            break
        b, ll = nb, nll
        if max(abs(t * s) for s in step) < 1e-6:
            break
    cov = _inv(H)
    return {"b": b, "se": [math.sqrt(max(cov[i][i], 0)) for i in range(p)], "ll": ll}


def _auc(scores: list[float], y: list[int]) -> float | None:
    pos = sum(y)
    neg = len(y) - pos
    if not pos or not neg:
        return None
    order = sorted(range(len(y)), key=lambda i: scores[i])
    rank_sum, i = 0.0, 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        r = (i + j) / 2 + 1
        rank_sum += r * sum(y[order[t]] for t in range(i, j + 1))
        i = j + 1
    return round((rank_sum - pos * (pos + 1) / 2) / (pos * neg), 3)


FEATURES = {   # 이름: (값 함수, 연속형 여부)
    "density": (lambda u, g: math.log1p(g["farms"]) if g.get("farms") else None, True),
    "heads_per_farm": (lambda u, g: math.log1p(g["hpf"]) if g.get("hpf") else None, True),
    "duck_share": (lambda u, g: g.get("duck"), True),
    "wild_nearby": (lambda u, g: 1.0 if u["wild14"] else 0.0, False),
    "neighbor_outbreak": (lambda u, g: 1.0 if u["neigh"] else 0.0, False),
    "temperature": (lambda u, g: u["temp"], True),
    "visits": (lambda u, g: u["visits"], True),
}


def _design(P: dict, feats: list[str], units: list[dict]):
    rows, ys, keep = [], [], []
    for u in units:
        g = P["regs"][u["k"]]
        v = [FEATURES[f][0](u, g) for f in feats]
        if any(x is None for x in v):
            continue
        rows.append(v); ys.append(u["y"]); keep.append(u)
    return rows, ys, keep


def model(species: str, P: dict) -> dict:
    cfg = SPECIES[species]
    past = [u for u in P["units"] if u["w"] < P["current"]]
    if sum(u["y"] for u in past) < MIN_POS:
        return {"available": False, "reason": "few_cases", "cases": sum(u["y"] for u in past)}
    base = [f for f in ("density", "heads_per_farm", "duck_share") if P["have_farms"]
            and sum(1 for g in P["regs"].values() if FEATURES[f][0]({}, g) is not None) >= len(P["regs"]) * 0.8
            and len({FEATURES[f][0]({}, g) for g in P["regs"].values()}) > 2]
    if cfg.get("wild") and sum(1 for u in past if u["wild14"]) >= MIN_UNITS:
        base.append("wild_nearby")
    if sum(1 for u in past if u["neigh"]) >= MIN_UNITS:
        base.append("neighbor_outbreak")
    if not base:
        return {"available": False, "reason": "few_factors"}
    cand = [base]
    if P.get("temp_cov") and P["temp_cov"]["cases"] >= MIN_POS:
        cand.append(base + ["temperature"])
    if P.get("visit_cov") and P["visit_cov"]["cases"] >= MIN_POS:
        cand.append(cand[-1] + ["visits"])
    fits = []
    unstable = []
    for feats in cand:
        rows, ys, keep = _design(P, feats, past)
        if sum(ys) < max(MIN_POS, EPV * len(feats)):     # 요인 하나당 발생 주 10개 이상(과적합 방지)
            continue
        cont = [FEATURES[f][1] for f in feats]
        clip = []
        for j in range(len(feats)):              # 극단값(몇몇 시군의 아주 큰 값)이 계산을 흔들지 않게 1·99 백분위로 자름
            if cont[j]:
                srt = sorted(r[j] for r in rows)
                clip.append((srt[int(len(srt) * 0.01)], srt[min(len(srt) - 1, int(len(srt) * 0.99))]))
            else:
                clip.append(None)
        rows = [[min(max(v, clip[j][0]), clip[j][1]) if clip[j] else v for j, v in enumerate(r)] for r in rows]
        mu = [sum(r[j] for r in rows) / len(rows) for j in range(len(feats))]
        sd = [math.sqrt(sum((r[j] - mu[j]) ** 2 for r in rows) / len(rows)) for j in range(len(feats))]
        if any(c and v < 1e-6 for c, v in zip(cont, sd)):        # 값이 거의 같은 연속 요인은 뺀다
            continue
        z = lambda r: [1.0] + [(r[j] - mu[j]) / sd[j] if cont[j] else r[j] for j in range(len(feats))]  # noqa: E731
        X = [z(r) for r in rows]
        f = logit_fit(X, ys)
        if any(abs(b) > math.log(1000) for b in f["b"][1:]):
            unstable.append(feats)
            continue
        sc = [sum(b * x for b, x in zip(f["b"], xi)) for xi in X]
        # 시간 검증: 마지막 완결 시즌을 빼고 학습 → 그 시즌 예측력
        sm = cfg["season_start_month"]
        seas = sorted({A.season_of(u["w"], sm) for u in keep})
        oot = None
        if len(seas) >= 3:
            test_s = seas[-1] if A.season_of(date.today(), sm) != seas[-1] else seas[-2]
            tr = [i for i, u in enumerate(keep) if A.season_of(u["w"], sm) < test_s]
            te = [i for i, u in enumerate(keep) if A.season_of(u["w"], sm) == test_s]
            if sum(ys[i] for i in tr) >= MIN_POS and sum(ys[i] for i in te) >= 3:
                f2 = logit_fit([X[i] for i in tr], [ys[i] for i in tr])
                s2 = [sum(b * x for b, x in zip(f2["b"], X[i])) for i in te]
                oot = {"season": A.season_label(test_s, sm), "auc": _auc(s2, [ys[i] for i in te]),
                       "cases": sum(ys[i] for i in te)}
        coef = []
        for j, name in enumerate(feats):
            b, se = f["b"][j + 1], f["se"][j + 1]
            coef.append({"factor": name, "continuous": cont[j], "sd": round(sd[j], 3), "mean": round(mu[j], 3),
                         "or": round(math.exp(b), 2), "lo": round(math.exp(b - 1.96 * se), 2),
                         "hi": round(math.exp(b + 1.96 * se), 2), "sig": abs(b) > 1.96 * se, "b": b})
        fits.append({"features": feats, "units": len(rows), "cases": sum(ys), "auc": _auc(sc, ys), "oot": oot,
                     "from": min(u["w"] for u in keep).isoformat(), "to": max(u["w"] for u in keep).isoformat(),
                     "coef": coef, "_f": f, "_mu": mu, "_sd": sd, "_cont": cont, "_clip": clip})
    if not fits:
        return {"available": False, "reason": "unstable" if unstable else "few_cases"}
    # 이번 주 예측: 이번 주 요인이 모두 있는 가장 풍부한 모형
    cur = [u for u in P["units"] if u["w"] == P["current"]]
    pred, used = [], None
    order = sorted(range(len(fits)), key=lambda i: (fits[i]["cases"] >= 100, i), reverse=True)
    for m in (fits[i] for i in order):
        rows, _, keep = _design(P, m["features"], cur)
        if len(rows) >= len(cur) * 0.8 and rows:
            used = m
            for r, u in zip(rows, keep):
                rc = [min(max(v, m["_clip"][j][0]), m["_clip"][j][1]) if m["_clip"][j] else v for j, v in enumerate(r)]
                x = [1.0] + [(rc[j] - m["_mu"][j]) / m["_sd"][j] if m["_cont"][j] else rc[j] for j in range(len(rc))]
                contrib = {m["features"][j]: m["_f"]["b"][j + 1] * x[j + 1] for j in range(len(r))}
                g = P["regs"][u["k"]]
                top = max(contrib.items(), key=lambda kv: kv[1])
                pred.append({"region": u["k"], "sigungu": g["sigungu"], "lat": round(g["lat"], 4), "lon": round(g["lon"], 4),
                             "p": _sig(sum(b * xi for b, xi in zip(m["_f"]["b"], x))), "top": top[0] if top[1] > 0 else None,
                             "wild": bool(u["wild14"]), "neigh": bool(u["neigh"])})
            break
    if pred:
        avg = sum(x["p"] for x in pred) / len(pred)
        for x in pred:
            x["rel"] = round(x["p"] / avg, 2) if avg else None
            x["p"] = round(x["p"], 5)
        pred.sort(key=lambda x: -x["p"])
    clean = [{k: v for k, v in m.items() if not k.startswith("_")} for m in fits]
    for m in clean:
        for c in m["coef"]:
            c.pop("b", None)
    # 화면 대표 모형: 요인이 더 많은 모형이라도 발생 주가 100개 미만(짧은 기간)이면 기간이 긴 기본 모형을 앞세운다
    best = max((i for i, m in enumerate(clean) if m["cases"] >= 100), default=0)
    return {"available": True, "models": clean, "best": best, "pred": pred,
            "pred_model": fits.index(used) if used else None, "week": P["current"].isoformat()}


# ── 실행·캐시 ───────────────────────────────────────────────────────────
def _weather_running() -> bool:
    from . import weather
    return bool(weather.state.get("running"))


def _stamp() -> tuple:
    with db.session() as con:
        # 차량 방문은 과거 자료를 받는 동안 몇 분마다 하루씩 늘어나므로 30일 단위로만 다시 계산한다
        return (con.scalar("SELECT COUNT(*) FROM event"),
                (con.scalar("SELECT COUNT(*) FROM vehicle_visit_day") or 0) // 30,
                (con.scalar("SELECT COUNT(*) FROM context_ts WHERE source = 'kma_asos_daily' AND metric = 'temp_avg'") or 0) // 20000,
                _weather_running(),
                str(con.scalar("SELECT MAX(collected_at) FROM region_stat"))[:10], con.scalar("SELECT COUNT(*) FROM bird_track"),
                date.today().isoformat())


def compute(species: str, disease: str | None) -> dict:
    t0 = time.time()
    P = _panel(species, disease)
    out = {"species": species, "disease": disease, "months": P["months"], "regions": len(P["regs"]),
           "units": sum(1 for u in P["units"] if u["w"] < P.get("current", date.max)),
           "cases": sum(u["y"] for u in P["units"]),
           "seasons": MAX_SEASONS}
    if not P["units"]:
        return {**out, "empty": True, "sec": round(time.time() - t0, 1)}
    for name, fn in (("model", lambda: model(species, P)), ("wild", lambda: wild_lag(P)),
                     ("temp", lambda: temperature(P)), ("visits", lambda: visit_dose(species, P)),
                     ("density", lambda: density(P)), ("birds", lambda: birds_timing(species, P))):
        try:
            out[name] = fn()
        except Exception as e:  # 한 분석이 실패해도 나머지는 보여 준다
            traceback.print_exc()
            out[name] = {"available": False, "reason": "error", "error": str(e)[:200]}
    out["sec"] = round(time.time() - t0, 1)
    return out


def _run(key, stamp):
    try:
        res = compute(*key)
        with _lock:
            _cache[key] = (stamp, res)
    except Exception as e:
        traceback.print_exc()
        with _lock:
            _cache[key] = (stamp, {"error": str(e)[:300]})
    finally:
        with _lock:
            _jobs.pop(key, None)


def get(species: str, disease: str | None, wait: float = 0) -> dict:
    """결과가 있으면 돌려주고, 없거나 데이터가 바뀌었으면 백그라운드에서 다시 계산한다."""
    key = (species, disease)
    stamp = _stamp()
    with _lock:
        hit = _cache.get(key)
        running = key in _jobs
        if (not hit or hit[0] != stamp) and not running:
            th = threading.Thread(target=_run, args=(key, stamp), name=f"assoc-{species}", daemon=True)
            _jobs[key] = time.time()
            th.start()
            running = True
    if wait and running:
        end = time.time() + wait
        while time.time() < end:
            with _lock:
                if key not in _jobs:
                    break
            time.sleep(0.1)
        with _lock:
            hit = _cache.get(key)
            running = key in _jobs
    if hit:
        return {**hit[1], "status": "updating" if running else "ready"}
    return {"status": "computing", "since": _jobs.get(key)}
