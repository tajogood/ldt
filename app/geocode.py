"""주소 → 좌표 (국토교통부 브이월드 주소좌표 변환 API), 결과는 geo_cache 에 보관.

가축 질병 발생 정보의 소재지는 '전라남도 나주시 봉황면 덕림리'처럼 리(里) 단위라
지번 검색(type=parcel)으로 그 리의 대표 지번 좌표를 받는다. → 화면에는 "리 대표점"으로 표시.
"""
from __future__ import annotations

import time

import httpx

from . import db
from .settings import VWORLD_DOMAIN, VWORLD_KEY

URL = "https://api.vworld.kr/req/address"
SEARCH = "https://api.vworld.kr/req/search"


def lookup(client: httpx.Client, address: str) -> tuple[str, float | None, float | None]:
    for typ in ("parcel", "road"):
        params = {"service": "address", "request": "getcoord", "version": "2.0", "crs": "epsg:4326",
                  "format": "json", "errorformat": "json", "type": typ, "address": address, "key": VWORLD_KEY}
        if VWORLD_DOMAIN:
            params["domain"] = VWORLD_DOMAIN
        r = client.get(URL, params=params)
        r.raise_for_status()
        resp = r.json().get("response", {})
        if resp.get("status") == "OK":
            p = resp["result"]["point"]
            return "OK", float(p["y"]), float(p["x"])
        if resp.get("status") == "ERROR":
            raise RuntimeError(f"브이월드 오류: {resp.get('error')}")
    return "NOT_FOUND", None, None


def lookup_with_fallback(client: httpx.Client, address: str) -> tuple[str, float | None, float | None, str]:
    """리 주소로 못 찾으면 한 단계씩 줄여(읍면동 → 시군구) 대표점을 찾는다. 4번째 값은 실제로 찾은 주소."""
    parts = address.split()
    for n in range(len(parts), 1, -1):
        a = " ".join(parts[:n])
        status, lat, lon = lookup(client, a)
        if status == "OK":
            return status, lat, lon, a
    return "NOT_FOUND", None, None, address


def run(limit: int = 2000, pause: float = 0.05) -> dict:
    """좌표가 없는 이벤트의 주소를 변환해 event.lat/lon 을 채운다. 이미 시도한 주소는 캐시를 쓴다."""
    if not VWORLD_KEY:
        raise RuntimeError("VWORLD_KEY 가 없습니다")
    with db.session() as con:
        # 캐시에 있는 좌표부터 반영
        con.execute("UPDATE event SET lat = (SELECT lat FROM geo_cache g WHERE g.address = event.place AND g.status='OK'), "
                    "lon = (SELECT lon FROM geo_cache g WHERE g.address = event.place AND g.status='OK') "
                    "WHERE lat IS NULL AND place IN (SELECT address FROM geo_cache WHERE status='OK')")
        todo = [r["place"] for r in con.rows(
            "SELECT place, COUNT(*) AS n FROM event WHERE lat IS NULL AND place IS NOT NULL AND place <> '' "
            "AND place NOT IN (SELECT address FROM geo_cache) GROUP BY place ORDER BY MAX(date) DESC LIMIT ?", [limit])]
        # 축산차량 방문 소재지(리)도 같은 캐시로 — 방문이 많은 곳부터
        if len(todo) < limit:
            todo += [r["address"] for r in con.rows(
                "SELECT p.address FROM vehicle_visit v JOIN visit_place p ON p.id = v.place_id "
                "WHERE p.address NOT IN (SELECT address FROM geo_cache) "
                "GROUP BY p.address ORDER BY SUM(v.n) DESC LIMIT ?", [limit - len(todo)]) if r["address"] not in todo]
    ok = miss = 0
    with httpx.Client(timeout=20) as c:
        for addr in todo:
            try:
                status, lat, lon, matched = lookup_with_fallback(c, addr)
            except RuntimeError:
                raise
            except Exception as e:  # 네트워크 등 일시 오류는 기록하고 다음에 다시 시도
                status, lat, lon = "ERROR", None, None
                print(f"  ! {addr}: {e}")
            with db.session() as con:
                if status != "ERROR":
                    db.upsert(con, "geo_cache", [{"address": addr, "lat": lat, "lon": lon, "status": status,
                                                  "provider": "vworld" if matched == addr else f"vworld~{len(matched.split())}",
                                                  "at": db.now()}])
                if status == "OK":
                    con.execute("UPDATE event SET lat=?, lon=? WHERE place=? AND lat IS NULL", [lat, lon, addr])
            ok += status == "OK"
            miss += status == "NOT_FOUND"
            time.sleep(pause)
    with db.session() as con:
        remaining = con.scalar("SELECT COUNT(DISTINCT place) FROM event WHERE lat IS NULL AND place IS NOT NULL "
                               "AND place NOT IN (SELECT address FROM geo_cache)")
    return {"tried": len(todo), "ok": ok, "not_found": miss, "remaining": remaining}


# ── 행정구역 대표점(읍면동·시군구) — 농림부 지역 통계를 지도에 올릴 때 사용 ──────────
def district(client: httpx.Client, sido: str, sigungu: str, emd: str = "") -> tuple[str, float | None, float | None]:
    """브이월드 검색 API(type=district)로 읍면동(L4) 또는 시군구(L2) 대표점을 찾는다."""
    sg = [t for t in (sigungu or "").split() if t]
    name = emd or (sg[0] if sg else "")
    if not name:
        return "NOT_FOUND", None, None
    params = {"service": "search", "request": "search", "version": "2.0", "crs": "EPSG:4326", "size": 100, "page": 1,
              "type": "district", "category": "L4" if emd else "L2", "format": "json", "errorformat": "json",
              "query": name, "key": VWORLD_KEY}
    if VWORLD_DOMAIN:
        params["domain"] = VWORLD_DOMAIN
    r = client.get(SEARCH, params=params)
    r.raise_for_status()
    resp = r.json().get("response", {})
    if resp.get("status") == "ERROR":
        raise RuntimeError(f"브이월드 오류: {resp.get('error')}")
    items = (resp.get("result") or {}).get("items") or []

    def score(it: dict) -> int:
        t = it.get("title", "")
        return (t.split()[-1:] == [name]) * 4 + all(x in t for x in sg) * 2 + (bool(sido) and sido[:2] in t)

    best = max(items, key=score, default=None)
    if best and best["title"].split()[-1] == name and (all(x in best["title"] for x in sg) or not emd):
        p = best["point"]
        return "OK", float(p["y"]), float(p["x"])
    return "NOT_FOUND", None, None


def regions(limit: int = 6000, pause: float = 0.03) -> dict:
    """농림부 읍면동 농장 통계의 지역마다 대표점을 geo_cache 에 넣는다(시군구 대표점도 함께)."""
    from .analysis import FARM_SOURCE, district_key
    if not VWORLD_KEY:
        raise RuntimeError("VWORLD_KEY 가 없습니다")
    with db.session() as con:
        rows = con.rows("SELECT DISTINCT sido, sigungu, emd FROM region_stat WHERE source = ? AND sigungu <> ''", [FARM_SOURCE])
        done = {r["address"] for r in con.rows("SELECT address FROM geo_cache WHERE address LIKE 'district:%'")}
    keys: dict[str, tuple] = {}
    for r in rows:
        keys.setdefault(district_key(r["sido"], r["sigungu"]), (r["sido"], r["sigungu"], ""))
        if r["emd"]:
            keys.setdefault(district_key(r["sido"], r["sigungu"], r["emd"]), (r["sido"], r["sigungu"], r["emd"]))
    todo = [(k, v) for k, v in keys.items() if k not in done][:limit]
    ok = miss = 0
    with httpx.Client(timeout=20) as c:
        for k, (sd, sg, emd) in todo:
            try:
                status, lat, lon = district(c, sd, sg, emd)
            except RuntimeError:
                raise
            except Exception as e:
                print(f"  ! {k}: {e}")
                continue
            with db.session() as con:
                db.upsert(con, "geo_cache", [{"address": k, "lat": lat, "lon": lon, "status": status,
                                              "provider": "vworld-district", "at": db.now()}])
            ok += status == "OK"
            miss += status == "NOT_FOUND"
            time.sleep(pause)
    return {"tried": len(todo), "ok": ok, "not_found": miss, "total": len(keys)}


# ── 읍면동 경계(지오펜스) — 농장 좌표가 없을 때 읍면동 단위로 농장 수·사육두수를 면으로 보여 준다 ──────
DATA = "https://api.vworld.kr/req/data"

# 농림부 자료의 옛 시도명도 브이월드 시도 코드로 맞춘다(전남·광주 통합, 강원·전북 특별자치도 포함)
SIDO_CODE = [("서울", "11"), ("부산", "26"), ("대구", "27"), ("인천", "28"), ("광주", "12"), ("전라남", "12"), ("전남", "12"),
             ("대전", "30"), ("울산", "31"), ("세종", "36"), ("경기", "41"), ("강원", "51"), ("충청북", "43"), ("충북", "43"),
             ("충청남", "44"), ("충남", "44"), ("전라북", "52"), ("전북", "52"), ("경상북", "47"), ("경북", "47"),
             ("경상남", "48"), ("경남", "48"), ("제주", "50")]


def sido_code(name: str | None) -> str:
    n = (name or "").strip()
    return next((c for p, c in SIDO_CODE if n.startswith(p)), "")


_DATA_REF: dict = {}


def _data(client: httpx.Client, layer: str, attr: str, geometry: bool, page: int = 1) -> tuple[list, int]:
    params = {"service": "data", "request": "GetFeature", "data": layer, "key": VWORLD_KEY, "attrFilter": attr,
              "geometry": "true" if geometry else "false", "attribute": "true", "crs": "EPSG:4326",
              "format": "json", "size": 1000, "page": page}
    if VWORLD_DOMAIN:
        params["domain"] = VWORLD_DOMAIN
    # 데이터 API는 Referer(등록 도메인)를 본다: 등록 도메인 → 브이월드 자체 순서로 시도
    resp = {}
    for ref in [x for x in (_DATA_REF.get("ok"), VWORLD_DOMAIN, "https://api.vworld.kr/", "https://www.vworld.kr/") if x]:
        r = client.get(DATA, params=params, timeout=60, headers={"Referer": ref})
        r.raise_for_status()
        resp = r.json().get("response", {})
        if (resp.get("error") or {}).get("code") == "INCORRECT_KEY":
            continue
        _DATA_REF["ok"] = ref
        break
    if resp.get("status") == "NOT_FOUND":
        return [], 1
    if resp.get("status") != "OK":
        raise RuntimeError(f"브이월드 데이터 API 오류: {resp.get('error')}")
    return resp["result"]["featureCollection"]["features"], int(resp.get("page", {}).get("total", 1))


def _dp(pts: list, tol: float) -> list:
    """Douglas–Peucker 단순화(도 단위)."""
    if len(pts) < 4:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = pts[a][:2], pts[b][:2]
        dx, dy = x2 - x1, y2 - y1
        nn = dx * dx + dy * dy
        best, idx = 0.0, -1
        for i in range(a + 1, b):
            px, py = pts[i][:2]
            if nn == 0:
                d = (px - x1) ** 2 + (py - y1) ** 2
            else:
                t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / nn))
                d = (px - x1 - t * dx) ** 2 + (py - y1 - t * dy) ** 2
            if d > best:
                best, idx = d, i
        if idx >= 0 and best > tol * tol:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(pts, keep) if k]


def simplify(geom: dict, tol: float = 0.0006) -> dict:
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    out = []
    for poly in polys:
        rings = []
        for ring in poly:
            r = [[round(x, 5), round(y, 5)] for x, y in (p[:2] for p in _dp(ring, tol))]
            if len(r) >= 4:
                rings.append(r)
        if rings:
            out.append(rings)
    return {"type": "MultiPolygon", "coordinates": out}


def shapes(max_age_days: int = 180) -> dict:
    """전국 읍면동 경계를 받아 region_shape 에 넣는다. 이미 받은 시군은 max_age_days 동안 다시 받지 않는다."""
    import json
    from datetime import timedelta
    if not VWORLD_KEY:
        raise RuntimeError("VWORLD_KEY 가 없습니다")
    cut = db.now() - timedelta(days=max_age_days)
    with db.session() as con:
        have = {r["c"] for r in con.rows("SELECT DISTINCT SUBSTR(code, 1, 5) AS c FROM region_shape WHERE at >= ?", [cut])}
    with httpx.Client(timeout=60) as c:
        sigs, _ = _data(c, "LT_C_ADSIGG_INFO", "sig_cd:>:0", False)
        todo = [f["properties"] for f in sigs if f["properties"]["sig_cd"] not in have]
        n = 0
        for sg in todo:
            page, pages, rows = 1, 1, []
            while page <= pages:
                feats, pages = _data(c, "LT_C_ADEMD_INFO", f"emd_cd:like:{sg['sig_cd']}", True, page)
                for f in feats:
                    p, g = f["properties"], simplify(f["geometry"])
                    ring = g["coordinates"][0][0] if g["coordinates"] else []
                    lon = sum(x for x, _ in ring) / len(ring) if ring else None
                    lat = sum(y for _, y in ring) / len(ring) if ring else None
                    rows.append({"code": p["emd_cd"], "sido_cd": p["emd_cd"][:2], "sigungu": sg["sig_kor_nm"],
                                 "emd": p["emd_kor_nm"], "full_nm": p.get("full_nm"), "lat": lat, "lon": lon,
                                 "geom": json.dumps(g, separators=(",", ":")), "at": db.now()})
                page += 1
            if rows:
                with db.session() as con:
                    db.upsert(con, "region_shape", rows)
                n += len(rows)
            time.sleep(0.05)
    return {"sigungu": len(todo), "townships": n}
