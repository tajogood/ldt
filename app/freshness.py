"""데이터 최신성 — 모든 조회 결과에 "어느 원천의, 언제까지의 데이터를, 언제 수집했는가"를 붙인다.

각 항목:
  source        원천 ID
  title/title_en 원천 이름
  rows          이번 조회에 쓰인 행 수
  data_from/to  데이터 자체의 기간(발생일·관측일 기준). 시설처럼 날짜가 없는 데이터는 None
  collected_at  DB에 적재된 가장 최근 시각(UTC ISO)
  refresh       기대 갱신 주기(daily/weekly/monthly/yearly/manual)
  stale         기대 주기의 2배가 지나도록 다시 수집되지 않았으면 True
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import db

REFRESH_HOURS = {"hourly": 1, "daily": 24, "weekly": 24 * 7, "monthly": 24 * 31, "yearly": 24 * 366, "manual": None}
MANUAL = {"manual_ui": ("화면 직접 입력", "Manual entry (UI)", "manual")}


def _source_meta() -> dict[str, dict]:
    from .collect import load_sources  # 순환 import 방지

    meta = {s["id"]: {"title": s["title"], "title_en": s.get("title_en", s["title"]),
                      "refresh": s.get("refresh", "manual" if s["provider"] == "file" else "daily")}
            for s in load_sources()}
    for k, (ko, en, r) in MANUAL.items():
        meta[k] = {"title": ko, "title_en": en, "refresh": r}
    return meta


def _age_hours(ts: str | None) -> float | None:
    if not ts:
        return None
    t = datetime.fromisoformat(ts)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - t).total_seconds() / 3600, 1)


def describe(source: str, rows: int, data_from: str | None, data_to: str | None, collected_at: str | None,
             meta: dict | None = None) -> dict:
    m = (meta or _source_meta()).get(source, {"title": source, "title_en": source, "refresh": "manual"})
    age = _age_hours(collected_at)
    limit = REFRESH_HOURS.get(m["refresh"])
    return {"source": source, "title": m["title"], "title_en": m["title_en"], "rows": rows,
            "data_from": data_from, "data_to": data_to, "collected_at": collected_at, "age_hours": age,
            "refresh": m["refresh"], "stale": bool(limit and age is not None and age > 2 * limit)}


def of(con: db.DB, table: str, where: str = "1=1", params: list | tuple = (), date_col: str | None = "date") -> list[dict]:
    """조회 조건 그대로 원천별 기간·수집시각을 집계."""
    dsel = f"MIN({date_col}) AS dfrom, MAX({date_col}) AS dto" if date_col else "NULL AS dfrom, NULL AS dto"
    rs = con.rows(f"SELECT source, COUNT(*) AS n, {dsel}, MAX(collected_at) AS cat FROM {table} "
                  f"WHERE {where} GROUP BY source ORDER BY source", params)
    meta = _source_meta()
    return [describe(r["source"], r["n"], r["dfrom"], r["dto"], r["cat"], meta) for r in rs]


def visits(con: db.DB) -> list[dict]:
    """축산차량 방문: 받은 날(vehicle_visit_day) 기준 기간·건수·수집시각."""
    r = con.rows("SELECT COUNT(*) AS n, MIN(date) AS dfrom, MAX(date) AS dto, MAX(at) AS cat, SUM(visits) AS v "
                 "FROM vehicle_visit_day")[0]
    if not r["n"]:
        return []
    d = describe("kahis_vehicle_visit", r["v"] or 0, r["dfrom"], r["dto"], r["cat"], _source_meta())
    d["days"] = r["n"]
    return [d]


def merge(*lists: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for lst in lists:
        for f in lst:
            cur = out.get(f["source"])
            if not cur:
                out[f["source"]] = dict(f)
                continue
            cur["rows"] += f["rows"]
            for k, fn in (("data_from", min), ("data_to", max), ("collected_at", max)):
                vals = [v for v in (cur[k], f[k]) if v]
                cur[k] = fn(vals) if vals else None
            cur["age_hours"] = _age_hours(cur["collected_at"])
            cur["stale"] = cur["stale"] or f["stale"]
    return sorted(out.values(), key=lambda x: x["source"])


def overview() -> list[dict]:
    """원천별 전체 현황: 마지막 수집 시도·성공, 데이터 기간."""
    meta = _source_meta()
    with db.session() as con:
        parts = [of(con, "event"), of(con, "facility", date_col=None), of(con, "contact", date_col="at"),
                 of(con, "context_ts"), of(con, "region_stat", date_col="as_of"), visits(con),
                 of(con, "bird_track", date_col="depart")]
        logs = {r["source"]: r for r in con.rows(
            "SELECT source, MAX(started_at) AS last_try, MAX(CASE WHEN ok THEN finished_at END) AS last_ok "
            "FROM collect_log GROUP BY source")}
    data = {f["source"]: f for f in merge(*parts)}
    out = []
    for sid in sorted(set(meta) | set(data)):
        f = data.get(sid) or describe(sid, 0, None, None, None, meta)
        f.update({"last_try": logs.get(sid, {}).get("last_try"), "last_ok": logs.get(sid, {}).get("last_ok")})
        out.append(f)
    return out
