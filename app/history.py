"""역대 분석: 여러 시즌·연도에 걸친 발생 패턴과 축산차량 방문의 관계.

모두 event(가축 질병 발생 정보) · vehicle_visit(KAHIS 축산차량 방문)에서 계산한다.
- season_compare : 시즌별 누적 발생 곡선(시즌 시작 후 주차) + 시즌 요약표
- region_matrix  : 시군 × 시즌 발생 수(반복 발생 지역)
- month_matrix   : 연도 × 월 발생 수(계절성)
- disease_years  : 질병 × 연도 발생 수(축종 안에서 질병별 추이)
- repeat_places  : 여러 시즌에 반복 발생한 리
- visit_case_control : 발생 리의 발생 전 21일 차량 방문 vs 같은 기간 다른 리 평균(차량유형별 배율)
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta

from . import analysis as A
from . import db
from .species import SPECIES


def _events(species: str, disease: str | None, kinds=("outbreak",)) -> list[dict]:
    where, params = A.species_filter(species)
    dw, dp = A._disease_filter(disease)
    ks = ",".join(f"'{k}'" for k in kinds)
    with db.session() as con:
        return con.rows(f"SELECT id, kind, disease, date, sido, sigungu, place, lat, heads FROM event "
                        f"WHERE {where} AND {dw} AND kind IN ({ks}) ORDER BY date", params + dp)


def _sm(species: str) -> int:
    return SPECIES[species]["season_start_month"]


def season_compare(species: str, disease: str | None, max_seasons: int = 12) -> dict:
    sm = _sm(species)
    evs = _events(species, disease)
    by: dict[int, list] = defaultdict(list)
    for e in evs:
        by[A.season_of(A._d(e["date"]), sm)].append(e)
    years = sorted(by)[-max_seasons:]
    today = date.today()
    series, rows = [], []
    for y in years:
        f, t = A.season_bounds(y, sm)
        d0 = A._d(f)
        weeks = Counter((A._d(e["date"]) - d0).days // 7 for e in by[y])
        last_w = min(52, ((min(A._d(t), today) - d0).days // 7))
        acc, cum = 0, []
        for w in range(0, last_w + 1):
            acc += weeks.get(w, 0)
            cum.append(acc)
        pk = max(weeks.items(), key=lambda x: x[1]) if weeks else (None, 0)
        dates = [A._d(e["date"]) for e in by[y]]
        series.append({"year": y, "label": A.season_label(y, sm), "cum": cum, "weekly": [weeks.get(w, 0) for w in range(len(cum))]})
        rows.append({"year": y, "label": A.season_label(y, sm), "outbreaks": len(by[y]),
                     "first": min(dates).isoformat(), "last": max(dates).isoformat(),
                     "first_week": (min(dates) - d0).days // 7, "duration": (max(dates) - min(dates)).days + 1,
                     "peak_week": pk[0], "peak_n": pk[1],
                     "peak_date": (d0 + timedelta(weeks=pk[0])).isoformat() if pk[0] is not None else None,
                     "sido": len({e["sido"] for e in by[y] if e["sido"]}),
                     "sigungu": len({(e["sido"], e["sigungu"]) for e in by[y] if e["sigungu"]}),
                     "heads": sum(e["heads"] or 0 for e in by[y]),
                     "in_progress": A._d(t) >= today})
    # '같은 시점까지' = 오늘이 이번 시즌의 몇 주차인지 구해, 지난 시즌들도 그 주차까지의 누적만 센다
    cur_year = A.season_of(today, sm)
    now_w = (today - A._d(A.season_bounds(cur_year, sm)[0])).days // 7
    for s_, r in zip(series, rows):
        r["by_now"] = s_["cum"][min(now_w, len(s_["cum"]) - 1)] if s_["cum"] else 0
    return {"species": species, "disease": disease, "season_start_month": sm, "series": series, "rows": rows,
            "unit": "week", "now_week": now_w, "current": A.season_label(cur_year, sm),
            "current_in_data": any(r["year"] == cur_year for r in rows)}


def region_matrix(species: str, disease: str | None, top: int = 30, max_seasons: int = 12) -> dict:
    sm = _sm(species)
    cnt: dict = defaultdict(Counter)
    for e in _events(species, disease):
        if not e["sigungu"]:
            continue
        cnt[f"{e['sido'] or ''} {e['sigungu']}".strip()][A.season_of(A._d(e["date"]), sm)] += 1
    years = sorted({y for c in cnt.values() for y in c})[-max_seasons:]
    regions = sorted(cnt, key=lambda r: (-sum(1 for y in years if cnt[r][y]), -sum(cnt[r][y] for y in years)))[:top]
    return {"seasons": [{"year": y, "label": A.season_label(y, sm)} for y in years],
            "rows": [{"region": r, "counts": [cnt[r][y] for y in years], "total": sum(cnt[r][y] for y in years),
                      "seasons_hit": sum(1 for y in years if cnt[r][y])} for r in regions]}


def month_matrix(species: str, disease: str | None, years: int = 15) -> dict:
    cnt: dict = defaultdict(Counter)
    for e in _events(species, disease):
        d = A._d(e["date"])
        cnt[d.year][d.month] += 1
    ys = sorted(cnt)[-years:]
    return {"years": ys, "rows": [{"year": y, "counts": [cnt[y][m] for m in range(1, 13)], "total": sum(cnt[y].values())} for y in ys],
            "month_total": [sum(cnt[y][m] for y in ys) for m in range(1, 13)]}


def disease_years(species: str, years: int = 15) -> dict:
    cnt: dict = defaultdict(Counter)
    for e in _events(species, None):
        cnt[e["disease"] or "?"][A._d(e["date"]).year] += 1
    ys = sorted({y for c in cnt.values() for y in c})[-years:]
    ds = sorted(cnt, key=lambda d: -sum(cnt[d][y] for y in ys))
    return {"years": ys, "rows": [{"disease": d, "counts": [cnt[d][y] for y in ys], "total": sum(cnt[d][y] for y in ys)}
                                  for d in ds if sum(cnt[d][y] for y in ys)]}


def repeat_places(species: str, disease: str | None, min_seasons: int = 2, limit: int = 60) -> dict:
    sm = _sm(species)
    by: dict = defaultdict(list)
    for e in _events(species, disease):
        if e["place"]:
            by[e["place"]].append(e)
    rows = []
    for p, es in by.items():
        ss = sorted({A.season_of(A._d(e["date"]), sm) for e in es})
        if len(ss) >= min_seasons:
            rows.append({"place": p, "seasons": [A.season_label(y, sm) for y in ss], "n_seasons": len(ss), "events": len(es),
                         "last": max(e["date"] for e in es), "lat": es[-1]["lat"] is not None})
    rows.sort(key=lambda r: (-r["n_seasons"], -r["events"], r["place"]))
    return {"rows": rows[:limit], "total": len(rows)}


def visit_case_control(species: str, disease: str | None, window: int = 21) -> dict:
    """발생 리의 발생 전 window 일 방문(차량유형별, 리·일 평균)과 같은 기간 전체 리 평균을 비교한 배율.

    배율 > 1 이면 발생 직전 그 리에 그 유형 차량이 평소(같은 기간 다른 리)보다 많이 드나들었다는 뜻.
    관찰 자료로 본 연관일 뿐 원인 판단은 아님(규모가 큰 농장 지역일수록 방문이 많음).
    """
    with db.session() as con:
        cov = con.rows("SELECT MIN(date) AS lo, MAX(date) AS hi, COUNT(*) AS n FROM vehicle_visit_day")[0]
    if not cov["n"]:
        return {"coverage": None, "cases": 0, "rows": []}
    lo, hi = A._d(str(cov["lo"])[:10]), A._d(str(cov["hi"])[:10])
    evs = [e for e in _events(species, disease) if e["place"] and lo + timedelta(days=window) <= A._d(e["date"]) <= hi]
    kid = A._vkind_ids(species)
    kc = A._kind_cond(kid)
    with db.session() as con:
        kinds = {r["id"]: r["vehicle"] for r in con.rows("SELECT id, vehicle FROM visit_kind")}
        pmap = {}
        places = list({e["place"] for e in evs})
        for i in range(0, len(places), 500):
            ch = places[i:i + 500]
            for r in con.rows(f"SELECT id, address FROM visit_place WHERE address IN ({','.join('?' * len(ch))})", ch):
                pmap[r["address"]] = r["id"]
        # 날짜별 전체 합계(차량유형별)와 방문 기록이 있는 리 수 — 한 번만 읽어 둔다
        day_kind: dict = defaultdict(Counter)
        for r in con.rows(f"SELECT v.date, v.kind_id, SUM(v.n) AS n FROM vehicle_visit v WHERE 1=1{kc} GROUP BY v.date, v.kind_id"):
            day_kind[str(r["date"])[:10]][kinds.get(r["kind_id"], "?")] += r["n"]
        day_places = {str(r["date"])[:10]: r["n"] for r in con.rows(
            f"SELECT v.date, COUNT(DISTINCT v.place_id) AS n FROM vehicle_visit v WHERE 1=1{kc} GROUP BY v.date")}
        case_tot: Counter = Counter()
        base_tot: Counter = Counter()
        case_vd = base_vd = 0          # 분모: 리·일 수
        used = 0
        for e in sorted(evs, key=lambda x: x["date"], reverse=True):
            pid = pmap.get(e["place"])
            if not pid:
                continue
            d1 = A._d(e["date"]) - timedelta(days=1)
            d0 = d1 - timedelta(days=window - 1)
            for r in con.rows(f"SELECT v.kind_id, SUM(v.n) AS n FROM vehicle_visit v WHERE v.place_id = ? "
                              f"AND v.date BETWEEN ? AND ?{kc} GROUP BY v.kind_id", [pid, d0, d1]):
                case_tot[kinds.get(r["kind_id"], "?")] += r["n"]
            case_vd += window
            d = d0
            while d <= d1:              # 같은 날 다른 리 평균(그날 방문 기록이 있는 리 기준)
                k = d.isoformat()
                if day_places.get(k):
                    for v, n in day_kind[k].items():
                        base_tot[v] += n / day_places[k]
                    base_vd += 1
                d += timedelta(days=1)
            used += 1
            if used >= 500:             # 계산량 제한(최근 500건)
                break
    rows = []
    for v in set(case_tot) | set(base_tot):
        c = case_tot[v] / case_vd if case_vd else 0
        b = base_tot[v] / base_vd if base_vd else 0
        rows.append({"vehicle": v, "case_per_day": round(c, 3), "base_per_day": round(b, 3),
                     "ratio": round(c / b, 2) if b else None, "case_total": case_tot[v]})
    rows.sort(key=lambda r: -(r["case_total"]))
    return {"coverage": {"from": lo.isoformat(), "to": hi.isoformat(), "days": cov["n"]}, "window": window,
            "cases": used, "candidates": len(evs), "rows": rows[:20]}
