"""데이터 조회 화면용: 원천(API)별로 수집된 데이터를 표·원본·항목 요약으로 보여 준다.

- 컬럼·정렬·필터는 표마다 정해 둔 목록(SPEC) 안에서만 받는다(SQL 주입 방지).
- 개인정보로 볼 수 있는 원본 항목(농장주 이름, 전화번호 등)은 원본 보기에서도 가린다.
"""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Iterator

from . import analysis, collect, db, freshness

# 표별 정의: from 절, 컬럼(이름 → SQL 식, 형식), 날짜 컬럼, 필터용 항목(facet), 기본 정렬, 원천 조건 여부
SPEC: dict[str, dict] = {
    "event": {
        "from": "event", "by_source": True, "date": "date", "order": "date DESC, id DESC",
        "cols": {"id": ("id", "num"), "date": ("date", "date"), "kind": ("kind", "text"), "disease": ("disease", "text"),
                 "species": ("species", "text"), "subspecies": ("subspecies", "text"), "sido": ("sido", "text"),
                 "sigungu": ("sigungu", "text"), "place": ("place", "text"), "legal_code": ("legal_code", "text"),
                 "lat": ("lat", "num"), "lon": ("lon", "num"), "heads": ("heads", "num"), "note": ("note", "text"),
                 "collected_at": ("collected_at", "time")},
        "facets": ["kind", "disease", "species", "sido"], "search": ["disease", "place", "sido", "sigungu", "note", "subspecies"],
    },
    "facility": {
        "from": "facility", "by_source": True, "date": None, "order": "sido, sigungu, id",
        "cols": {"id": ("id", "num"), "kind": ("kind", "text"), "species": ("species", "text"),
                 "subspecies": ("subspecies", "text"), "name": ("name", "text"), "status": ("status", "text"),
                 "size": ("size", "num"), "sido": ("sido", "text"), "sigungu": ("sigungu", "text"),
                 "address": ("address", "text"), "lat": ("lat", "num"), "lon": ("lon", "num"),
                 "collected_at": ("collected_at", "time")},
        "facets": ["kind", "species", "status", "sido"], "search": ["name", "address", "sido", "sigungu", "subspecies"],
    },
    "contact": {
        "from": "contact", "by_source": True, "date": "at", "order": "at DESC",
        "cols": {"id": ("id", "num"), "at": ("at", "time"), "farm_key": ("farm_key", "text"), "purpose": ("purpose", "text"),
                 "party": ("party", "text"), "collected_at": ("collected_at", "time")},
        "facets": ["purpose"], "search": ["farm_key", "party", "purpose"],
    },
    "context_ts": {
        "from": "context_ts", "by_source": True, "date": "date", "order": "date DESC, place, metric",
        "cols": {"date": ("date", "date"), "place": ("place", "text"), "metric": ("metric", "text"),
                 "value": ("value", "num"), "lat": ("lat", "num"), "lon": ("lon", "num"),
                 "collected_at": ("collected_at", "time")},
        "facets": ["place", "metric"], "search": ["place", "metric"],
    },
    "region_stat": {
        "from": "region_stat", "by_source": True, "date": "as_of", "order": "as_of DESC, sido, sigungu, emd, metric",
        "cols": {"as_of": ("as_of", "text"), "sido": ("sido", "text"), "sigungu": ("sigungu", "text"), "emd": ("emd", "text"),
                 "species": ("species", "text"), "metric": ("metric", "text"), "value": ("value", "num"),
                 "collected_at": ("collected_at", "time")},
        "facets": ["sido", "species", "metric"], "search": ["sido", "sigungu", "emd", "metric"],
    },
    "bird_track": {
        "from": "bird_track", "by_source": True, "date": "depart", "order": "depart DESC, id DESC",
        "cols": {"id": ("id", "num"), "year": ("year", "num"), "depart": ("depart", "date"), "species": ("species", "text"),
                 "tag": ("tag", "text"), "origin": ("origin", "text"), "dest": ("dest", "text"), "dest_country": ("dest_country", "text"),
                 "dest_precision": ("dest_precision", "text"), "distance_km": ("distance_km", "num"), "bearing": ("bearing", "num"),
                 "compass": ("compass", "text"), "clock": ("clock", "num"), "origin_lat": ("origin_lat", "num"),
                 "origin_lon": ("origin_lon", "num"), "dest_lat": ("dest_lat", "num"), "dest_lon": ("dest_lon", "num")},
        "facets": ["year", "species", "dest_country", "compass"], "search": ["species", "tag", "origin", "dest"],
    },
    "vehicle_visit": {
        "from": analysis.VV, "by_source": False, "date": "v.date", "order": "v.date DESC, v.n DESC",
        "cols": {"date": ("v.date", "date"), "address": ("p.address", "text"), "sido": ("p.sido", "text"),
                 "sigungu": ("p.sigungu", "text"), "facility": ("k.facility", "text"), "species": ("k.species", "text"),
                 "species_raw": ("k.species_raw", "text"), "vehicle": ("k.vehicle", "text"), "n": ("v.n", "num"),
                 "first_hour": ("v.first_hour", "num"), "last_hour": ("v.last_hour", "num")},
        "facets": ["sido", "facility", "vehicle", "species"], "search": ["address", "facility", "vehicle", "species_raw"],
    },
}

# 원본 보기에서 가리는 항목: 사람 이름·연락처로 볼 수 있는 것
_MASK = re.compile(r"(FARM_?NM|FRMHS_?NM|OWNER|RPRSV|CEO|TELNO|TEL_?NO|PHONE|MBTLNUM|대표자|성명|전화|휴대)", re.I)


def _src(sid: str) -> dict:
    if sid == "manual_ui":
        return {"id": sid, "target": "event", "title": "화면 직접 입력", "title_en": "Manual entry", "provider": "manual"}
    return collect.get_source(sid)


def catalog() -> dict:
    """원천 목록 + 원천별 정규화 행 수·원본 건수·기간·마지막 수집."""
    fr = {f["source"]: f for f in freshness.overview()}
    with db.session() as con:
        raw = {r["source"]: r for r in con.rows("SELECT source, COUNT(*) AS n, MAX(fetched_at) AS at FROM raw_record GROUP BY source")}
        last = {r["source"]: r for r in con.rows(
            "SELECT source, finished_at, ok, rows, message FROM collect_log WHERE id IN (SELECT MAX(id) FROM collect_log GROUP BY source)")}
    out = []
    for s in collect.load_sources():
        f = fr.get(s["id"], {})
        out.append({"id": s["id"], "title": s["title"], "title_en": s.get("title_en", s["title"]), "provider": s["provider"],
                    "group": s.get("group") or "", "target": s["target"], "refresh": s.get("refresh"),
                    "configured": collect.is_configured(s), "enabled": s.get("enabled", True) is not False,
                    "rows": f.get("rows", 0), "data_from": f.get("data_from"), "data_to": f.get("data_to"),
                    "collected_at": f.get("collected_at"), "stale": f.get("stale"),
                    "raw": (raw.get(s["id"]) or {}).get("n", 0), "last": last.get(s["id"]),
                    "service": s.get("service_id") or s.get("endpoint") or s.get("path") or ""})
    return {"sources": out}


def _where(spec: dict, sid: str, q: str | None, date_from: str | None, date_to: str | None,
           filters: dict[str, str]) -> tuple[str, list]:
    w, p = ["1=1"], []
    if spec["by_source"]:
        w.append("source = ?")
        p.append(sid)
    if q:
        cols = [spec["cols"][c][0] for c in spec["search"]]
        w.append("(" + " OR ".join(f"CAST({c} AS VARCHAR) LIKE ?" for c in cols) + ")")
        p += [f"%{q}%"] * len(cols)
    dc = spec["date"]
    if dc and date_from:
        w.append(f"{dc} >= ?")
        p.append(date_from)
    if dc and date_to:
        w.append(f"{dc} <= ?" if dc != "at" else f"{dc} < ?")
        p.append(date_to if dc != "at" else date_to + " 23:59:59")
    for k, v in filters.items():
        if k in spec["cols"] and v not in (None, ""):
            if v == "∅":
                w.append(f"({spec['cols'][k][0]} IS NULL OR CAST({spec['cols'][k][0]} AS VARCHAR) = '')")
            else:
                w.append(f"CAST({spec['cols'][k][0]} AS VARCHAR) = ?")
                p.append(v)
    return " AND ".join(w), p


def table(sid: str, q: str | None = None, date_from: str | None = None, date_to: str | None = None,
          filters: dict | None = None, sort: str | None = None, desc: bool = True,
          limit: int = 100, offset: int = 0) -> dict:
    src = _src(sid)
    t = src["target"]
    spec = SPEC[t]
    where, params = _where(spec, sid, q, date_from, date_to, filters or {})
    order = f"{spec['cols'][sort][0]} {'DESC' if desc else 'ASC'}" if sort in spec["cols"] else spec["order"]
    sel = ", ".join(f"{expr} AS {name}" for name, (expr, _) in spec["cols"].items())
    with db.session() as con:
        total = con.scalar(f"SELECT COUNT(*) FROM {spec['from']} WHERE {where}", params)
        items = con.rows(f"SELECT {sel} FROM {spec['from']} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                         params + [min(limit, 1000), offset])
    return {"source": sid, "title": src["title"], "title_en": src.get("title_en", src["title"]), "table": t,
            "columns": [{"name": n, "type": ty} for n, (_, ty) in spec["cols"].items()],
            "facets": spec["facets"], "has_date": bool(spec["date"]), "has_raw": t in ("event", "facility", "contact"),
            "total": total, "items": items, "sort": sort, "desc": desc}


def facet(sid: str, col: str, q: str | None = None, date_from: str | None = None, date_to: str | None = None,
          filters: dict | None = None, limit: int = 60) -> dict:
    """한 컬럼의 값별 건수(필터 선택 목록·분포 확인용). 다른 필터는 반영하고 자기 자신은 뺀다."""
    src = _src(sid)
    spec = SPEC[src["target"]]
    if col not in spec["cols"]:
        raise KeyError(col)
    f = {k: v for k, v in (filters or {}).items() if k != col}
    where, params = _where(spec, sid, q, date_from, date_to, f)
    expr = spec["cols"][col][0]
    with db.session() as con:
        rows = con.rows(f"SELECT {expr} AS v, COUNT(*) AS n FROM {spec['from']} WHERE {where} GROUP BY {expr} "
                        f"ORDER BY COUNT(*) DESC LIMIT ?", params + [limit])
        distinct = con.scalar(f"SELECT COUNT(DISTINCT {expr}) FROM {spec['from']} WHERE {where}", params)
    return {"column": col, "distinct": distinct, "values": [{"v": r["v"], "n": r["n"]} for r in rows]}


def timeline(sid: str, q: str | None = None, date_from: str | None = None, date_to: str | None = None,
             filters: dict | None = None) -> dict:
    """날짜 컬럼이 있으면 월별(기간이 짧으면 일별) 건수."""
    src = _src(sid)
    spec = SPEC[src["target"]]
    dc = spec["date"]
    if not dc:
        return {"unit": None, "items": []}
    if src["target"] == "vehicle_visit" and not q and not filters:
        # 축산차량 방문은 행이 많아 날짜별 합계표(vehicle_visit_day)로 그린다
        w, p = ["1=1"], []
        if date_from:
            w.append("date >= ?"); p.append(date_from)
        if date_to:
            w.append("date <= ?"); p.append(date_to)
        with db.session() as con:
            rows = con.rows(f"SELECT date AS k, visits AS n FROM vehicle_visit_day WHERE {' AND '.join(w)} ORDER BY date", p)
        if len(rows) > 400:      # 길면 월별로
            agg: dict = {}
            for r in rows:
                agg[str(r["k"])[:7]] = agg.get(str(r["k"])[:7], 0) + (r["n"] or 0)
            return {"unit": "month", "items": [{"k": k, "n": v} for k, v in agg.items()]}
        return {"unit": "day", "items": [{"k": str(r["k"])[:10], "n": r["n"]} for r in rows]}
    where, params = _where(spec, sid, q, date_from, date_to, filters or {})
    with db.session() as con:
        lo, hi = con.rows(f"SELECT MIN({dc}) AS lo, MAX({dc}) AS hi FROM {spec['from']} WHERE {where}", params)[0].values()
        if not lo:
            return {"unit": None, "items": []}
        span = (analysis._d(str(hi)[:10]) - analysis._d(str(lo)[:10])).days
        n = 10 if span <= 120 else 7 if span <= 3 * 365 else 4
        key = f"SUBSTR(CAST({dc} AS VARCHAR), 1, {n})"
        cnt = "SUM(v.n)" if src["target"] == "vehicle_visit" else "COUNT(*)"
        rows = con.rows(f"SELECT {key} AS k, {cnt} AS n FROM {spec['from']} WHERE {where} GROUP BY {key} ORDER BY {key}", params)
    return {"unit": {10: "day", 7: "month", 4: "year"}[n], "items": [{"k": r["k"], "n": r["n"]} for r in rows]}


def profile(sid: str) -> dict:
    """항목 요약: 컬럼별 비어 있는 비율·서로 다른 값 수·최소/최대."""
    src = _src(sid)
    spec = SPEC[src["target"]]
    since = None
    if src["target"] == "vehicle_visit":   # 행이 많아 최근 7일로 요약
        with db.session() as con:
            last = con.scalar("SELECT MAX(date) FROM vehicle_visit_day")
        if last:
            since = (analysis._d(str(last)[:10]) - analysis.timedelta(days=6)).isoformat()
    where, params = _where(spec, sid, None, since, None, {})
    out = []
    cols = list(spec["cols"].items())
    # 한 번 훑어서 모든 항목을 같이 계산(항목마다 따로 읽으면 느림)
    sel = ", ".join(f"COUNT({e}) AS f{i}, COUNT(DISTINCT {e}) AS d{i}, MIN({e}) AS lo{i}, MAX({e}) AS hi{i}"
                    for i, (_, (e, _t)) in enumerate(cols))
    with db.session() as con:
        r = con.rows(f"SELECT COUNT(*) AS total, {sel} FROM {spec['from']} WHERE {where}", params)[0]
    total = r["total"] or 0
    for i, (name, (_e, ty)) in enumerate(cols):
        out.append({"name": name, "type": ty, "filled": r[f"f{i}"], "empty": total - (r[f"f{i}"] or 0),
                    "distinct": r[f"d{i}"], "min": r[f"lo{i}"], "max": r[f"hi{i}"]})
    return {"total": total, "columns": out, "since": since}


def _mask(o):
    if isinstance(o, dict):
        return {k: ("●●●" if _MASK.search(str(k)) and v not in (None, "") else _mask(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [_mask(v) for v in o]
    return o


def raw(sid: str, q: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    """API가 준 원본 레코드(가장 최근 수집부터). 사람 이름·연락처 항목은 가린다."""
    w, p = "source = ?", [sid]
    if q:
        w += " AND CAST(payload AS VARCHAR) LIKE ?" if db.engine.dialect.name != "postgresql" else " AND payload::text LIKE ?"
        p.append(f"%{q}%")
    with db.session() as con:
        total = con.scalar(f"SELECT COUNT(*) FROM raw_record WHERE {w}", p)
        rows = con.rows(f"SELECT id, fetched_at, batch, payload FROM raw_record WHERE {w} ORDER BY id DESC LIMIT ? OFFSET ?",
                        p + [min(limit, 500), offset])
        batches = con.rows("SELECT batch, COUNT(*) AS n, MAX(fetched_at) AS at FROM raw_record WHERE source = ? "
                           "GROUP BY batch ORDER BY batch DESC LIMIT 10", [sid])
    fields: dict[str, int] = {}
    for r in rows:
        pl = r["payload"]
        if isinstance(pl, str):
            try:
                pl = json.loads(pl)
            except ValueError:
                pass
        r["payload"] = _mask(pl if isinstance(pl, dict) else {"value": pl})
        for k in r["payload"]:
            fields[k] = fields.get(k, 0) + 1
    return {"source": sid, "total": total, "items": rows, "fields": list(fields), "batches": batches}


def record_raw(table_name: str, rid: int) -> dict:
    """정규화된 한 행의 원본(raw 컬럼) — 행을 눌렀을 때 상세 보기."""
    if table_name not in ("event", "facility", "contact"):
        raise KeyError(table_name)
    with db.session() as con:
        rows = con.rows(f"SELECT * FROM {table_name} WHERE id = ?", [rid])
    if not rows:
        raise KeyError(rid)
    r = rows[0]
    raw_ = r.get("raw")
    if isinstance(raw_, str):
        try:
            raw_ = json.loads(raw_)
        except ValueError:
            pass
    r["raw"] = _mask(raw_)
    return r


def csv_rows(sid: str, q: str | None, date_from: str | None, date_to: str | None, filters: dict,
             sort: str | None, desc: bool, max_rows: int = 200_000) -> Iterator[str]:
    """CSV 내려받기(엑셀에서 한글이 깨지지 않게 BOM). 1만 행씩 끊어 읽는다."""
    first = True
    off = 0
    while off < max_rows:
        r = table(sid, q, date_from, date_to, filters, sort, desc, limit=1000, offset=off)
        buf = io.StringIO()
        w = csv.writer(buf)
        if first:
            buf.write("﻿")
            w.writerow([c["name"] for c in r["columns"]])
            first = False
        for it in r["items"]:
            w.writerow([it.get(c["name"]) for c in r["columns"]])
        yield buf.getvalue()
        if len(r["items"]) < 1000:
            break
        off += 1000
