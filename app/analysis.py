"""연관성 분석 — 방역대 영향, 접촉 역추적, 시공간 연쇄, 위험지수, 주간 추이."""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from . import db, geo
from .species import SPECIES

WILD_KINDS = ("wild_detection",)
OUTBREAK_KINDS = ("outbreak",)


def _d(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def species_filter(species: str | None) -> tuple[str, list]:
    """축종 탭 → event.species 조건. 해당 축종의 야생 이벤트(야생조류/멧돼지)도 함께 본다."""
    if not species or species == "all":
        return "1=1", []
    cfg = SPECIES[species]
    sp = [species] + ([cfg["wild"]] if cfg.get("wild") else [])
    return f"species IN ({','.join('?' * len(sp))})", sp


# ── 방역대 영향 ─────────────────────────────────────────────────────────
def facilities_within(lat: float, lon: float, km: float, species: str | None = None,
                      kinds: tuple[str, ...] | None = None) -> list[dict]:
    a, b, c, d = geo.bbox(lat, lon, km)
    sql = "SELECT * FROM facility WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
    params: list = [a, b, c, d]
    if species and species != "all":
        sql += " AND (species = ? OR species IS NULL OR kind != 'farm')"
        params.append(species)
    if kinds:
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        params += list(kinds)
    with db.session() as con:
        cand = db.rows(con, sql, params)
    out = []
    for f in cand:
        dist = geo.haversine_km(lat, lon, f["lat"], f["lon"])
        if dist <= km:
            f.pop("raw", None)
            f["dist_km"] = round(dist, 2)
            out.append(f)
    return sorted(out, key=lambda f: f["dist_km"])


def impact(event_id: int, species: str | None = None) -> dict:
    with db.session() as con:
        ev = db.rows(con, "SELECT * FROM event WHERE id = ?", [event_id])
    if not ev:
        raise KeyError(event_id)
    ev = ev[0]
    ev.pop("raw", None)
    sp = species or {"wild_bird": "poultry", "wild_boar": "pig"}.get(ev["species"], ev["species"])
    radii = SPECIES.get(sp, SPECIES["poultry"])["radii_km"]
    if ev["lat"] is None:
        return {"event": ev, "radii_km": radii, "zones": None, "farms": [], "facilities": [],
                "message": "이 이벤트에는 좌표가 없어 반경 분석을 할 수 없습니다(시군 단위로만 표시)."}
    within = facilities_within(ev["lat"], ev["lon"], radii["surveillance"], sp)
    farms = [f for f in within if f["kind"] == "farm"]
    zones = {}
    for name, km in radii.items():
        fs = [f for f in farms if f["dist_km"] <= km]
        zones[name] = {"km": km, "farms": len(fs), "heads": sum(f["size"] or 0 for f in fs),
                       "facilities": sum(1 for f in within if f["kind"] != "farm" and f["dist_km"] <= km)}
    # 농장 좌표가 없을 때: 읍면동 대표점이 반경 안에 드는 읍면동의 농장·사육두수 합(추정치)
    dens = [d for d in farm_density(sp)["items"] if d["lat"] is not None]
    for name, km in radii.items():
        inside = [d for d in dens if geo.haversine_km(ev["lat"], ev["lon"], d["lat"], d["lon"]) <= km]
        zones[name]["est"] = {"townships": len(inside), "farms": sum(d["farms"] for d in inside),
                              "heads": sum(d["heads"] or 0 for d in inside),
                              "names": [d["emd"] or d["sigungu"] for d in inside][:12]}
    # 가장 가까운 거점소독시설 3곳(반경과 무관)
    with db.session() as con:
        hubs = db.rows(con, "SELECT name, address, status, lat, lon FROM facility WHERE kind='disinfection' AND lat IS NOT NULL")
    for h in hubs:
        h["dist_km"] = round(geo.haversine_km(ev["lat"], ev["lon"], h["lat"], h["lon"]), 1)
    hubs.sort(key=lambda h: h["dist_km"])
    # 같은 반경에서 이후 21일 안에 생긴 다른 이벤트(연쇄 후보)
    after = nearby_events(ev["lat"], ev["lon"], radii["surveillance"], ev["date"], days_after=21, exclude=ev["id"])
    return {"event": ev, "radii_km": radii, "zones": zones, "farms": farms[:200],
            "facilities": [f for f in within if f["kind"] != "farm"][:100], "following_events": after,
            "hubs": hubs[:3]}


def nearby_events(lat: float, lon: float, km: float, day: str, days_before: int = 0,
                  days_after: int = 0, exclude: int | None = None) -> list[dict]:
    lo = (_d(day) - timedelta(days=days_before)).isoformat()
    hi = (_d(day) + timedelta(days=days_after)).isoformat()
    a, b, c, d = geo.bbox(lat, lon, km)
    with db.session() as con:
        cand = db.rows(con, "SELECT id, kind, disease, species, date, place, sigungu, lat, lon FROM event "
                            "WHERE date BETWEEN ? AND ? AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                       [lo, hi, a, b, c, d])
    out = []
    for e in cand:
        if e["id"] == exclude:
            continue
        dist = geo.haversine_km(lat, lon, e["lat"], e["lon"])
        if dist <= km:
            e["dist_km"] = round(dist, 2)
            out.append(e)
    return sorted(out, key=lambda e: e["date"])


# ── 접촉 역추적 ─────────────────────────────────────────────────────────
def trace_contacts(farm_key: str, day: str, days: int = 21) -> dict:
    """발생 농장에 D-21~D0 진입한 업체(party)와, 같은 업체가 같은 기간 들른 다른 농장."""
    lo = (_d(day) - timedelta(days=days)).isoformat()
    hi = _d(day).isoformat() + " 23:59"
    with db.session() as con:
        visits = db.rows(con, "SELECT party, purpose, at FROM contact WHERE farm_key = ? AND at BETWEEN ? AND ? "
                              "ORDER BY at", [farm_key, lo, hi])
        parties = sorted({v["party"] for v in visits if v["party"]})
        linked = []
        if parties:
            linked = db.rows(con, f"SELECT farm_key, party, purpose, at FROM contact WHERE party IN "
                                  f"({','.join('?' * len(parties))}) AND farm_key != ? AND at BETWEEN ? AND ? "
                                  "ORDER BY at", parties + [farm_key, lo, hi])
    nodes = [{"id": f"farm:{farm_key}", "type": "source_farm", "label": farm_key}]
    nodes += [{"id": f"party:{p}", "type": "party", "label": p} for p in parties]
    other = sorted({l["farm_key"] for l in linked})
    nodes += [{"id": f"farm:{f}", "type": "farm", "label": f} for f in other]
    edges = [{"from": f"farm:{farm_key}", "to": f"party:{v['party']}", "at": v["at"], "purpose": v["purpose"]}
             for v in visits if v["party"]]
    edges += [{"from": f"party:{l['party']}", "to": f"farm:{l['farm_key']}", "at": l["at"], "purpose": l["purpose"]}
              for l in linked]
    return {"farm_key": farm_key, "window": [lo, day], "visits": visits, "linked_farms": other,
            "graph": {"nodes": nodes, "edges": edges}}


# ── 시공간 연쇄 ─────────────────────────────────────────────────────────
def spacetime_pairs(species: str | None, max_km: float = 20, max_days: int = 14,
                    since: str | None = None, until: str | None = None, disease: str | None = None) -> dict:
    """가까운 거리·짧은 시차로 이어진 발생 쌍과, 그것으로 묶인 클러스터.

    Knox 통계: 실제 근접 쌍 수 vs 시간을 섞었을 때의 기대 쌍 수.
    """
    where, params = species_filter(species)
    sql = f"SELECT id, kind, disease, species, date, place, lat, lon FROM event WHERE {where} AND lat IS NOT NULL"
    if since:
        sql += " AND date >= ?"
        params = params + [since]
    if until:
        sql += " AND date <= ?"
        params = params + [until]
    if disease and disease != "all":
        sql += " AND disease = ?"
        params = params + [disease]
    sql += " AND kind IN ('outbreak','wild_detection')"
    with db.session() as con:
        evs = db.rows(con, sql, params)
    pairs, close_space, close_time = [], 0, 0
    n = len(evs)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = evs[i], evs[j]
            dk = geo.haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
            dt = abs((_d(a["date"]) - _d(b["date"])).days)
            s, t = dk <= max_km, dt <= max_days
            close_space += s
            close_time += t
            if s and t:
                first, second = (a, b) if a["date"] <= b["date"] else (b, a)
                pairs.append({"from": first["id"], "to": second["id"], "km": round(dk, 1), "days": dt})
    total_pairs = n * (n - 1) / 2 or 1
    expected = close_space * close_time / total_pairs
    # union-find 클러스터
    parent = {e["id"]: e["id"] for e in evs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p in pairs:
        parent[find(p["from"])] = find(p["to"])
    clusters = defaultdict(list)
    for e in evs:
        clusters[find(e["id"])].append(e["id"])
    return {"pairs": pairs, "observed": len(pairs), "expected": round(expected, 2),
            "knox_ratio": round(len(pairs) / expected, 2) if expected else None,
            "clusters": [v for v in clusters.values() if len(v) > 1]}


# ── 주간 추이 ───────────────────────────────────────────────────────────
def weekly_series(species: str | None, since: str | None = None, until: str | None = None,
                  disease: str | None = None) -> list[dict]:
    where, params = species_filter(species)
    since = since or (date.today() - timedelta(days=180)).isoformat()
    until = until or "9999-12-31"
    dw, dp = _disease_filter(disease)
    with db.session() as con:
        evs = db.rows(con, f"SELECT kind, date FROM event WHERE {where} AND {dw} AND date BETWEEN ? AND ?",
                      params + dp + [since, until])
    buckets: dict[str, dict] = {}
    for e in evs:
        d = _d(e["date"])
        wk = (d - timedelta(days=d.weekday())).isoformat()
        b = buckets.setdefault(wk, {"week": wk, "outbreak": 0, "wild_detection": 0, "other": 0})
        k = e["kind"] if e["kind"] in ("outbreak", "wild_detection") else "other"
        b[k] += 1
    return [buckets[k] for k in sorted(buckets)]


# ── 위험지수 ────────────────────────────────────────────────────────────
def _pct_rank(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    srt = sorted(values.values())
    n = len(srt)
    return {k: (sum(1 for x in srt if x <= v) - 1) / max(n - 1, 1) for k, v in values.items()}


def risk_index(species: str, as_of: str | None = None) -> dict:
    """시군 단위 주간 위험 점수 = Σ 가중치 × 요인(0~1). 요인별 기여를 함께 돌려준다.

    데이터가 없는 요인은 0으로 두고 missing 에 표시한다(점수를 부풀리지 않음).
    """
    cfg = SPECIES[species]
    w = cfg["weights"]
    today = _d(as_of) if as_of else date.today()
    d21 = (today - timedelta(days=21)).isoformat()
    d30 = (today - timedelta(days=30)).isoformat()
    d7 = (today - timedelta(days=7)).isoformat()

    with db.session() as con:
        farms = db.rows(con, "SELECT sido, sigungu, size, lat, lon FROM facility WHERE kind='farm' AND species=? "
                             "AND lat IS NOT NULL AND (status IS NULL OR status NOT LIKE '%폐업%')", [species])
        wild = db.rows(con, "SELECT lat, lon, date FROM event WHERE kind IN ('wild_detection') AND species=? "
                            "AND date BETWEEN ? AND ? AND lat IS NOT NULL", [cfg.get("wild") or "-", d30, today.isoformat()])
        outbreaks = db.rows(con, "SELECT sigungu, lat, lon FROM event WHERE kind='outbreak' AND species=? AND date BETWEEN ? AND ?",
                            [species, d21, today.isoformat()])
        weather = db.rows(con, "SELECT place, metric, AVG(value) v FROM context_ts WHERE source='kma_asos_daily' "
                               "AND date BETWEEN ? AND ? AND metric IN ('temp_avg','humidity_avg') GROUP BY place, metric",
                          [d7, today.isoformat()])

    regions: dict[str, dict] = {}
    farm_source = "facility"
    if not farms:
        # 농장 좌표(행안부 인허가)가 없으면 농림부 읍면동 농장 통계를 시군 단위로 묶어 쓴다(규모 요인은 제외)
        for r in farm_density(species, "sigungu")["items"]:
            if r["lat"] is None:
                continue
            key = f"{r['sido']} {r['sigungu']}".strip()
            regions[key] = {"region": key, "sido": r["sido"], "sigungu": r["sigungu"], "farms": int(r["farms"]),
                            "sizes": [], "avg": r.get("avg"), "lat": r["lat"], "lon": r["lon"]}
        farm_source = "region_stat"
    for f in farms:
        key = f"{f['sido'] or ''} {f['sigungu'] or ''}".strip()
        r = regions.setdefault(key, {"region": key, "sido": f["sido"], "sigungu": f["sigungu"],
                                     "farms": 0, "sizes": [], "lat": 0.0, "lon": 0.0})
        r["farms"] += 1
        r["sizes"].append(f["size"] or 0)
        r["lat"] += f["lat"]
        r["lon"] += f["lon"]
    if farm_source == "facility":
        for r in regions.values():
            r["lat"] /= r["farms"]
            r["lon"] /= r["farms"]

    missing = []
    if not regions:
        return {"species": species, "as_of": today.isoformat(), "weights": w, "regions": [],
                "missing": ["farm"], "farm_source": None,
                "message": "농장 데이터(농장 좌표 또는 읍면동 농장 통계)가 아직 없습니다."}

    density = _pct_rank({k: r["farms"] for k, r in regions.items()})
    all_sizes = sorted(s for r in regions.values() for s in r["sizes"] if s)
    p75 = all_sizes[int(len(all_sizes) * 0.75)] if all_sizes else None
    large = {k: (sum(1 for s in r["sizes"] if p75 and s >= p75) / r["farms"]) for k, r in regions.items()}
    if farm_source == "region_stat" and any(r.get("avg") for r in regions.values()):
        # 농장별 규모가 없으면 시군 평균 사육두수(농장당)의 순위로 대신한다
        large = _pct_rank({k: r.get("avg") or 0 for k, r in regions.items()})
    elif not p75:
        missing.append("large_share")

    wildp = {}
    for k, r in regions.items():
        dmin = min((geo.haversine_km(r["lat"], r["lon"], e["lat"], e["lon"]) for e in wild), default=None)
        wildp[k] = math.exp(-dmin / 15) if dmin is not None else 0.0
    if not wild and w.get("wild_proximity"):
        missing.append("wild_proximity")

    ob = defaultdict(float)
    for o in outbreaks:
        for k, r in regions.items():
            if o["lat"] is not None:
                dist = geo.haversine_km(r["lat"], r["lon"], o["lat"], o["lon"])
                ob[k] += math.exp(-dist / 10)
            elif o["sigungu"] and o["sigungu"] == r["sigungu"]:
                ob[k] += 1
    recent = {k: min(ob[k], 3) / 3 for k in regions}
    if not outbreaks:
        missing.append("recent_outbreak")

    missing.append("contact")  # 농장 식별자 ↔ 좌표 연결 전까지는 미사용
    wx = {}
    if weather:
        temp = [x["v"] for x in weather if x["metric"] == "temp_avg" and x["v"] is not None]
        t = sum(temp) / len(temp) if temp else None
        if t is not None:
            val = max(0.0, min(1.0, (15 - t) / 15)) if species == "poultry" else \
                  max(0.0, min(1.0, (t - 15) / 15)) if species == "cattle" else 0.0
            wx = {k: val for k in regions}  # TODO: 관측소-시군 최근접 매칭으로 지역별 값
    else:
        missing.append("weather")

    out = []
    for k, r in regions.items():
        comp = {"density": density.get(k, 0), "large_share": large.get(k, 0), "wild_proximity": wildp.get(k, 0),
                "recent_outbreak": recent.get(k, 0), "contact": 0.0, "weather": wx.get(k, 0.0)}
        contrib = {f: round(w.get(f, 0) * v, 4) for f, v in comp.items()}
        out.append({"region": k, "sido": r["sido"], "sigungu": r["sigungu"], "farms": r["farms"],
                    "lat": round(r["lat"], 5), "lon": round(r["lon"], 5),
                    "score": round(sum(contrib.values()), 4), "contrib": contrib})
    out.sort(key=lambda x: -x["score"])
    return {"species": species, "as_of": today.isoformat(), "weights": w, "regions": out,
            "missing": sorted(set(missing)), "farm_source": farm_source}


# ── 농장 분포(농림부 읍면동 농장 통계 + 행정구역 대표점) ─────────────────
FARM_SOURCE = "mafra_farm_by_area"
HEADS_SOURCE = "mafra_farm_by_scale"


def district_key(sido: str, sigungu: str, emd: str = "") -> str:
    return f"district:{sido or ''}|{sigungu or ''}|{emd or ''}"


def farm_density(species: str | None, level: str = "emd") -> dict:
    """축종별 농장 수를 읍면동(또는 시군) 단위로 묶고 행정구역 대표점 좌표를 붙인다."""
    sw, sp = ("species = ?", [species]) if species and species != "all" else ("1=1", [])
    cols = "sido, sigungu, emd" if level == "emd" else "sido, sigungu"
    with db.session() as con:
        rows = con.rows(f"SELECT {cols}, SUM(value) AS farms FROM region_stat WHERE source = ? AND metric LIKE 'farms%' "
                        f"AND {sw} AND sigungu <> '' GROUP BY {cols}", [FARM_SOURCE] + sp)
        geo_rows = con.rows("SELECT address, lat, lon FROM geo_cache WHERE address LIKE 'district:%' AND status='OK'")
        as_of = con.scalar("SELECT MAX(collected_at) FROM region_stat WHERE source = ?", [FARM_SOURCE])
        heads = con.rows(f"SELECT {cols}, SUM(value) AS heads FROM region_stat WHERE source = ? AND metric LIKE 'heads%' "
                         f"AND {sw} AND sigungu <> '' GROUP BY {cols}", [HEADS_SOURCE] + sp)
    g = {r["address"]: (r["lat"], r["lon"]) for r in geo_rows}
    key = lambda r: (r["sido"], r["sigungu"], r.get("emd", ""))  # noqa: E731
    hd = {key(r): r["heads"] for r in heads}
    items = []
    for r in rows:
        if not r["farms"]:
            continue
        ll = g.get(district_key(r["sido"], r["sigungu"], r.get("emd", ""))) or \
            (g.get(district_key(r["sido"], r["sigungu"])) if level == "emd" else None)
        h = hd.get(key(r))
        items.append({**r, "farms": round(r["farms"]), "heads": round(h) if h is not None else None,
                      "avg": round(h / r["farms"]) if h is not None else None,
                      "lat": ll[0] if ll else None, "lon": ll[1] if ll else None})
    items.sort(key=lambda x: -x["farms"])
    return {"items": items, "level": level, "collected_at": as_of,
            "located": sum(1 for i in items if i["lat"] is not None),
            "has_heads": any(i["heads"] is not None for i in items)}


# ── 유행 시즌 단위 분석 ─────────────────────────────────────────────────
def season_of(day: date, start_month: int) -> int:
    """시즌의 시작 연도. 9월 시작이면 2025-10-05 → 2025, 2026-03-01 → 2025."""
    return day.year if day.month >= start_month else day.year - 1


def season_bounds(year: int, start_month: int) -> tuple[str, str]:
    a = date(year, start_month, 1)
    b = date(year + (1 if start_month > 1 else 1), start_month, 1) - timedelta(days=1) if start_month > 1 \
        else date(year, 12, 31)
    return a.isoformat(), b.isoformat()


def season_label(year: int, start_month: int) -> str:
    return f"{year}/{str(year + 1)[2:]}" if start_month > 1 else str(year)


def _disease_filter(disease: str | None) -> tuple[str, list]:
    if not disease or disease == "all":
        return "1=1", []
    return "disease = ?", [disease]


def seasons(species: str, disease: str | None = None) -> list[dict]:
    cfg = SPECIES[species]
    sm = cfg["season_start_month"]
    where, params = species_filter(species)
    dw, dp = _disease_filter(disease)
    with db.session() as con:
        rows = con.rows(f"SELECT date, lat FROM event WHERE {where} AND {dw} AND kind IN ('outbreak','wild_detection')",
                        params + dp)
    agg: dict[int, dict] = {}
    for r in rows:
        y = season_of(_d(r["date"]), sm)
        a = agg.setdefault(y, {"year": y, "events": 0, "with_coords": 0})
        a["events"] += 1
        a["with_coords"] += r["lat"] is not None
    out = []
    for y in sorted(agg, reverse=True):
        f, t = season_bounds(y, sm)
        out.append({**agg[y], "label": season_label(y, sm), "from": f, "to": t})
    return out


def season_events(species: str, year: int, disease: str | None = None,
                  link_days: int | None = None, link_km: float | None = None) -> dict:
    """한 시즌의 발생을 시간순으로, 각 발생의 '가장 가까운 선행 발생'(같은 시즌, link_days 이내)과 함께.

    - dist_index_km : 시즌 첫 발생(지표 사례)으로부터의 거리
    - prev          : link_days 이내 선행 발생 중 가장 가까운 것 {id, km, days}
    - linked        : prev 가 link_km 이내면 True (국지 전파 가능) / 아니면 원거리 유입·독립 발생
    - cluster       : linked 관계로 이어진 묶음 번호
    """
    cfg = SPECIES[species]
    sm = cfg["season_start_month"]
    link_days = link_days or cfg["link"]["days"]
    link_km = link_km or cfg["link"]["km"]
    f, t = season_bounds(year, sm)
    where, params = species_filter(species)
    dw, dp = _disease_filter(disease)
    with db.session() as con:
        evs = con.rows(f"SELECT id, kind, disease, species, subspecies, date, sido, sigungu, place, lat, lon, heads, source "
                       f"FROM event WHERE {where} AND {dw} AND kind IN ('outbreak','wild_detection') AND date BETWEEN ? AND ? "
                       f"ORDER BY date, id", params + dp + [f, t])
    geo_evs = [e for e in evs if e["lat"] is not None]
    index = next((e for e in geo_evs if e["kind"] == "outbreak"), geo_evs[0] if geo_evs else None)
    parent: dict[int, int] = {e["id"]: e["id"] for e in geo_evs}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    d0 = _d(f)
    linked_n = jump_n = 0
    for i, e in enumerate(geo_evs):
        de = _d(e["date"])
        e["day"] = (de - d0).days
        e["dist_index_km"] = round(geo.haversine_km(index["lat"], index["lon"], e["lat"], e["lon"]), 1) if index else None
        best = None
        for p in geo_evs[:i]:
            dd = (de - _d(p["date"])).days
            if dd > link_days or dd < 0:
                continue
            km = geo.haversine_km(p["lat"], p["lon"], e["lat"], e["lon"])
            if best is None or km < best["km"]:
                best = {"id": p["id"], "km": round(km, 1), "days": dd}
        e["prev"] = best
        e["linked"] = bool(best and best["km"] <= link_km)
        if e["linked"]:
            parent[find(e["id"])] = find(best["id"])
            linked_n += 1
        elif best is not None or i > 0:
            jump_n += 1
    clusters: dict[int, list[int]] = defaultdict(list)
    for e in geo_evs:
        clusters[find(e["id"])].append(e["id"])
    cid = {root: k + 1 for k, root in enumerate(sorted((r for r, m in clusters.items() if len(m) > 1),
                                                       key=lambda r: -len(clusters[r])))}
    for e in geo_evs:
        e["cluster"] = cid.get(find(e["id"]))
    for e in evs:
        e.setdefault("day", (_d(e["date"]) - d0).days)
    return {"species": species, "disease": disease or "all", "year": year, "label": season_label(year, sm),
            "from": f, "to": t, "link": {"days": link_days, "km": link_km},
            "index_case": {k: index[k] for k in ("id", "date", "place", "lat", "lon")} if index else None,
            "summary": {"events": len(evs), "with_coords": len(geo_evs), "linked": linked_n, "independent": jump_n,
                        "clusters": len(cid), "largest_cluster": max((len(clusters[r]) for r in cid), default=0)},
            "events": evs}


_shape_cache: dict = {}


def farm_shapes(species: str | None) -> dict:
    """읍면동 경계(GeoJSON) + 그 읍면동의 농장 수·사육두수. 경계와 통계는 (시도, 시군구, 읍면동) 이름으로 맞춘다."""
    import json
    from .geocode import sido_code
    with db.session() as con:
        stamp = (con.scalar("SELECT COUNT(*) FROM region_shape"), con.scalar("SELECT MAX(at) FROM region_shape"),
                 con.scalar("SELECT MAX(collected_at) FROM region_stat WHERE source IN (?, ?)", [FARM_SOURCE, HEADS_SOURCE]))
    ck = (species or "all",) + stamp
    if ck in _shape_cache:
        return _shape_cache[ck]
    with db.session() as con:
        shapes = con.rows("SELECT code, sido_cd, sigungu, emd, full_nm, geom FROM region_shape")
    k1, k2 = {}, defaultdict(list)
    for s in shapes:
        k1[(s["sido_cd"], s["sigungu"], s["emd"])] = s
        k2[(s["sido_cd"], s["sigungu"].split()[0], s["emd"])].append(s)
        k2[("", s["sigungu"].split()[0], s["emd"])].append(s)
    feats, unmatched = [], 0
    seen = set()
    for d in farm_density(species)["items"]:
        if not d.get("emd"):
            continue
        sc = sido_code(d["sido"])
        s = k1.get((sc, d["sigungu"], d["emd"]))
        if not s:
            cand = k2.get((sc, d["sigungu"].split()[0], d["emd"])) or k2.get(("", d["sigungu"].split()[0], d["emd"])) or []
            s = cand[0] if len(cand) == 1 else None
        if not s or s["code"] in seen:
            unmatched += 1
            continue
        seen.add(s["code"])
        feats.append({"type": "Feature", "geometry": json.loads(s["geom"]),
                      "properties": {"code": s["code"], "name": s["full_nm"] or f"{d['sido']} {d['sigungu']} {d['emd']}",
                                     "farms": d["farms"], "heads": d["heads"], "avg": d["avg"]}})
    out = {"type": "FeatureCollection", "features": feats, "unmatched": unmatched, "shapes": len(shapes)}
    _shape_cache.clear()
    _shape_cache[ck] = out
    return out


# ── 축산차량 시설방문(KAHIS) ─────────────────────────────────────────────
VV = ("vehicle_visit v JOIN visit_place p ON p.id = v.place_id JOIN visit_kind k ON k.id = v.kind_id")


def _vsp(species: str | None) -> tuple[str, list]:
    return ("k.species = ?", [species]) if species and species != "all" else ("1=1", [])


def visits_near_event(event_id: int, km: float = 3, days: int = 21) -> dict:
    """발생 전 days 일 동안 ① 발생 리 자체 ② 반경 km 안의 리(대표점 기준)에 다녀간 차량 방문 횟수."""
    with db.session() as con:
        ev = con.rows("SELECT id, date, place, lat, lon FROM event WHERE id = ?", [event_id])
    if not ev:
        raise KeyError(event_id)
    ev = ev[0]
    d1 = _d(ev["date"])
    d0 = d1 - timedelta(days=days)
    dist = {ev["place"]: 0.0} if ev["place"] else {}
    if ev["lat"] is not None:
        a, b, c, d = geo.bbox(ev["lat"], ev["lon"], km)
        with db.session() as con:
            for g in con.rows("SELECT address, lat, lon FROM geo_cache WHERE status='OK' AND lat BETWEEN ? AND ? "
                              "AND lon BETWEEN ? AND ? AND address NOT LIKE 'district:%'", [a, b, c, d]):
                dk = geo.haversine_km(ev["lat"], ev["lon"], g["lat"], g["lon"])
                if dk <= km:
                    dist.setdefault(g["address"], round(dk, 2))
    empty = {"event_id": event_id, "from": d0.isoformat(), "to": d1.isoformat(), "km": km, "total": 0, "days": days + 1,
             "places": [], "by_vehicle": [], "by_facility": [], "daily": [], "same_place": 0}
    with db.session() as con:
        empty["days_covered"] = con.scalar("SELECT COUNT(*) FROM vehicle_visit_day WHERE date BETWEEN ? AND ?", [d0, d1])
        if not dist:
            return empty
        ph = ",".join("?" * len(dist))
        pid = {r["id"]: r["address"] for r in con.rows(f"SELECT id, address FROM visit_place WHERE address IN ({ph})", list(dist))}
        if not pid:
            return empty
        kinds = {r["id"]: r for r in con.rows("SELECT id, facility, vehicle FROM visit_kind")}
        raw = con.rows(f"SELECT v.date, v.place_id, v.kind_id, v.n FROM vehicle_visit v WHERE v.date BETWEEN ? AND ? "
                       f"AND v.place_id IN ({','.join(str(int(i)) for i in pid)})", [d0, d1])
        rows = [{"date": r["date"], "address": pid[r["place_id"]], "facility": kinds[r["kind_id"]]["facility"],
                 "vehicle": kinds[r["kind_id"]]["vehicle"], "n": r["n"]} for r in raw]
    byv, byf, byd, byp = Counter(), Counter(), Counter(), Counter()
    for r in rows:
        byv[r["vehicle"]] += r["n"]
        byf[r["facility"]] += r["n"]
        byd[str(r["date"])[:10]] += r["n"]
        byp[r["address"]] += r["n"]
    return {**empty, "total": sum(byv.values()), "same_place": byp.get(ev["place"], 0),
            "by_vehicle": byv.most_common(), "by_facility": byf.most_common(), "daily": sorted(byd.items()),
            "places": [{"address": a, "n": n, "km": dist.get(a)} for a, n in byp.most_common(15)]}


def _vkind_ids(species: str | None) -> list[int] | None:
    if not species or species == "all":
        return None
    with db.session() as con:
        return [r["id"] for r in con.rows("SELECT id FROM visit_kind WHERE species = ?", [species])]


def _kind_cond(ids: list[int] | None) -> str:
    """축종 조건을 번호 목록으로 바꿔 큰 표를 조인하지 않고 거른다."""
    if ids is None:
        return ""
    return f" AND v.kind_id IN ({','.join(str(int(i)) for i in ids) or '-1'})"


def _visit_last(con) -> date:
    last = con.scalar("SELECT MAX(date) FROM vehicle_visit_day")
    return _d(str(last)[:10]) if last else date.today()


def visit_series(species: str | None, days: int = 60) -> dict:
    kc = _kind_cond(_vkind_ids(species))
    with db.session() as con:
        since = _visit_last(con) - timedelta(days=days)
        agg = con.rows(f"SELECT v.date, v.kind_id, SUM(v.n) AS n FROM vehicle_visit v WHERE v.date >= ?{kc} "
                       f"GROUP BY v.date, v.kind_id", [since])
        kinds = {r["id"]: r["vehicle"] for r in con.rows("SELECT id, vehicle FROM visit_kind")}
    tot: dict = {}
    for r in agg:
        k = (str(r["date"])[:10], kinds.get(r["kind_id"], "?"))
        tot[k] = tot.get(k, 0) + r["n"]
    return {"items": [{"date": d, "vehicle": v, "n": n} for (d, v), n in sorted(tot.items())]}


def visit_map(species: str | None, days: int = 7, limit: int = 4000) -> dict:
    """최근 days 일 리별 방문 합계 + 리 대표점(지오코딩 캐시). 좌표 없는 리는 개수만 알려 준다."""
    kc = _kind_cond(_vkind_ids(species))
    with db.session() as con:
        since = _visit_last(con) - timedelta(days=days - 1)
        # 큰 표는 날짜 범위로 먼저 리·종류별로 줄이고, 작은 결과에만 주소·좌표를 붙인다
        agg = con.rows(f"SELECT v.place_id, v.kind_id, SUM(v.n) AS n FROM vehicle_visit v WHERE v.date >= ?{kc} "
                       f"GROUP BY v.place_id, v.kind_id", [since])
        kinds = {r["id"]: r["vehicle"] for r in con.rows("SELECT id, vehicle FROM visit_kind")}
        pids = sorted({r["place_id"] for r in agg})
        places = {}
        for i in range(0, len(pids), 1000):
            chunk = ",".join(str(int(x)) for x in pids[i:i + 1000])
            for r in con.rows(f"SELECT p.id, p.address, g.lat, g.lon FROM visit_place p LEFT JOIN geo_cache g "
                              f"ON g.address = p.address AND g.status = 'OK' WHERE p.id IN ({chunk})"):
                places[r["id"]] = r
    tot: Counter = Counter()
    tv: dict = {}
    for r in agg:
        tot[r["place_id"]] += r["n"]
        tv.setdefault(r["place_id"], Counter())[kinds.get(r["kind_id"], "?")] += r["n"]
    items, unloc = [], 0
    for pid, n in tot.most_common():
        p = places.get(pid)
        if not p or p["lat"] is None:
            unloc += 1
            continue
        items.append({"address": p["address"], "n": n, "lat": p["lat"], "lon": p["lon"], "top": tv[pid].most_common(3)})
    return {"items": items[:limit], "unlocated": unloc, "since": since.isoformat()}
