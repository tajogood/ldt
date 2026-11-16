"""보고서: 시즌별 주요 분석을 한 묶음(JSON)으로 만들고, 자동으로 보관한다.

- build(species, disease, season)  : 화면의 '보고서'가 그리는 자료 전부(발생·주간 추이·시즌 비교·반복 지역·연관 분석·뉴스·자료 기준)
- 자동 보관(스냅숏)                : 매주 월요일 05:00(04:30 일일 수집 뒤) 진행 중인 시즌의 주간 보고서,
                                     시즌이 끝나면 그 시즌의 최종 보고서. 보관본은 만든 날의 숫자를 그대로 간직한다.
  주간 보관본은 시즌마다 최근 12개만 남기고, 최종·직접 보관본은 지우지 않는다.
시즌은 2025년(가금은 2025/26)부터 고를 수 있다.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone

from . import analysis as A
from . import assoc, db, freshness, history, news
from .species import SPECIES

FIRST_YEAR = int(os.environ.get("REPORT_FIRST_YEAR", "2025"))
AUTO = os.environ.get("REPORT_AUTO", "1") == "1"
AUTO_AT = os.environ.get("REPORT_AUTO_AT", "05:00")      # 월요일 이 시각 이후 첫 확인 때
KEEP_WEEKLY = 12
state: dict = {"last": None, "error": None}


def seasons(species: str) -> list[dict]:
    sm = SPECIES[species]["season_start_month"]
    cur = A.season_of(date.today(), sm)
    out = []
    for y in range(FIRST_YEAR, cur + 1):
        f, t = A.season_bounds(y, sm)
        out.append({"year": y, "label": A.season_label(y, sm), "from": f, "to": t, "in_progress": A._d(t) >= date.today()})
    return list(reversed(out))


def _events(species: str, disease: str | None, f: str, t: str) -> list[dict]:
    where, params = A.species_filter(species)
    dw, dp = A._disease_filter(disease)
    with db.session() as con:
        rows = con.rows(f"SELECT id, kind, disease, subspecies, date, sido, sigungu, place, lat, lon, heads FROM event "
                        f"WHERE {where} AND {dw} AND date BETWEEN ? AND ? "
                        f"AND kind IN ('outbreak', 'wild_detection', 'movement_restriction') ORDER BY date",
                        params + dp + [f, t])
    for r in rows:
        r["date"] = str(r["date"])[:10]
    return rows


def build(species: str, disease: str | None = None, season: int | None = None, assoc_wait: float = 0) -> dict:
    cfg = SPECIES[species]
    sm = cfg["season_start_month"]
    dis = disease if disease and disease != "all" else cfg["main_disease"]
    today = date.today()
    season = season if season is not None else A.season_of(today, sm)
    f, t = A.season_bounds(season, sm)
    p_f, p_t = A.season_bounds(season - 1, sm)
    in_progress = A._d(t) >= today
    upto = min(A._d(t), today)
    ev = _events(species, dis, f, t)
    pev = _events(species, dis, p_f, p_t)
    # 직전 시즌 '같은 기간'(시즌 시작 후 같은 날수까지)
    same = (A._d(p_f) + (upto - A._d(f))).isoformat()
    ob = [e for e in ev if e["kind"] == "outbreak"]
    kpi = {"outbreaks": len(ob), "wild_detections": sum(1 for e in ev if e["kind"] == "wild_detection"),
           "restrictions": sum(1 for e in ev if e["kind"] == "movement_restriction"),
           "sigungu": len({(e["sido"], e["sigungu"]) for e in ob if e["sigungu"]}),
           "heads": sum(e["heads"] or 0 for e in ob),
           "first": ob[0]["date"] if ob else None, "last": ob[-1]["date"] if ob else None,
           "prev_same": {"outbreaks": sum(1 for e in pev if e["kind"] == "outbreak" and e["date"] <= same),
                         "wild_detections": sum(1 for e in pev if e["kind"] == "wild_detection" and e["date"] <= same)},
           "prev_total": sum(1 for e in pev if e["kind"] == "outbreak")}
    a = assoc.get(species, dis, wait=assoc_wait)
    if a.get("status") in ("ready", "updating") and not in_progress and isinstance(a.get("model"), dict):
        a = {**a, "model": {**a["model"], "pred": []}}       # 지난 시즌 보고서에는 '이번 주 예측'을 넣지 않는다
    nw = news.query(species=species, since=None, limit=200)
    items = [n for n in nw["items"] if f <= str(n.get("published") or n.get("fetched_at") or "")[:10] <= t][:15]
    for n in items:
        for k in ("published", "fetched_at"):
            if n.get(k) is not None:
                n[k] = str(n[k])
    return {"species": species, "disease": dis, "season": season, "label": A.season_label(season, sm),
            "from": f, "to": t, "in_progress": in_progress, "upto": upto.isoformat(),
            "prev_label": A.season_label(season - 1, sm), "kpi": kpi,
            "events": ev, "prev_events": [e for e in pev if e["kind"] == "outbreak"],
            "weekly": A.weekly_series(species, f, upto.isoformat(), dis),
            "seasons": history.season_compare(species, dis), "regions": history.region_matrix(species, dis),
            "assoc": a, "news": items, "freshness": freshness.overview(),
            "generated_at": db.now().isoformat(timespec="seconds")}


# ── 보관(스냅숏) ─────────────────────────────────────────────────────────
def _default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    return str(o)


def save(species: str, kind: str, season: int | None = None, disease: str | None = None) -> dict:
    data = build(species, disease, season, assoc_wait=600)
    if data["assoc"].get("status") == "computing":       # 연관 분석이 10분 안에 안 끝나면 바로 계산
        data["assoc"] = {**assoc.compute(species, data["disease"]), "status": "ready"}
    ts = db.now()
    row = {"uid": f"{species}:{data['season']}:{kind}:{ts:%Y%m%d%H%M%S}", "species": species, "disease": data["disease"],
           "season": data["season"], "kind": kind, "created_at": ts, "data": json.dumps(data, ensure_ascii=False, default=_default)}
    with db.session() as con:
        db.upsert(con, "report_snapshot", [row])
        if kind == "weekly":                             # 주간 보관본은 시즌마다 최근 12개만
            ids = [r["id"] for r in con.rows("SELECT id FROM report_snapshot WHERE species = ? AND season = ? AND kind = 'weekly' "
                                             "ORDER BY created_at DESC", [species, data["season"]])]
            for old in ids[KEEP_WEEKLY:]:
                con.execute("DELETE FROM report_snapshot WHERE id = ?", [old])
    return {"species": species, "season": data["season"], "kind": kind}


def _local(ts) -> str:
    """DB 시각(UTC) → 서버 현지 시각 'YYYY-MM-DD HH:MM'."""
    if ts is None:
        return ""
    d = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone().strftime("%Y-%m-%d %H:%M")


def listing(species: str | None = None) -> list[dict]:
    w, p = ("WHERE species = ?", [species]) if species else ("", [])
    with db.session() as con:
        rows = con.rows(f"SELECT id, species, disease, season, kind, created_at FROM report_snapshot {w} "
                        f"ORDER BY season DESC, created_at DESC", p)
    for r in rows:
        sm = SPECIES.get(r["species"], {}).get("season_start_month", 1)
        r["label"] = A.season_label(r["season"], sm)
        r["created_at"] = _local(r["created_at"])
    return rows


def get(rid: int) -> dict | None:
    with db.session() as con:
        r = con.rows("SELECT id, kind, created_at, data FROM report_snapshot WHERE id = ?", [rid])
    if not r:
        return None
    d = json.loads(r[0]["data"])
    d["snapshot"] = {"id": r[0]["id"], "kind": r[0]["kind"], "created_at": _local(r[0]["created_at"])}
    return d


def _has(species: str, season: int, kind: str, since: datetime | None = None) -> bool:
    q, p = "SELECT COUNT(*) FROM report_snapshot WHERE species = ? AND season = ? AND kind = ?", [species, season, kind]
    if since:
        q += " AND created_at >= ?"
        p.append(since)
    with db.session() as con:
        return bool(con.scalar(q, p))


def _species_with_data() -> list[str]:
    with db.session() as con:
        return [sp for sp in SPECIES if con.scalar("SELECT COUNT(*) FROM event WHERE species = ? AND kind = 'outbreak'", [sp])]


def tick(now: datetime | None = None) -> list[dict]:
    """자동 보관 한 번 확인: 끝난 시즌의 최종본이 없으면 만들고, 월요일 AUTO_AT 이후 이번 주 주간본이 없으면 만든다."""
    now = now or datetime.now().astimezone()          # 서버 현지 시각(한국)
    if now.tzinfo is None:
        now = now.astimezone()
    done = []
    hh, mm = (int(x) for x in AUTO_AT.split(":"))
    for sp in _species_with_data():
        sm = SPECIES[sp]["season_start_month"]
        cur = A.season_of(now.date(), sm)
        for y in range(FIRST_YEAR, cur):                                   # 끝난 시즌 → 최종 보고서
            if not _has(sp, y, "final"):
                done.append(save(sp, "final", y))
        monday = datetime.combine(now.date() - timedelta(days=now.weekday()), datetime.min.time(), tzinfo=now.tzinfo)
        if now >= monday.replace(hour=hh, minute=mm) and not _has(sp, cur, "weekly", monday.astimezone(timezone.utc)):
            done.append(save(sp, "weekly", cur))
    return done


def _loop() -> None:
    time.sleep(120)
    while True:
        try:
            r = tick()
            if r:
                state["last"] = {"at": db.now().isoformat(timespec="seconds"), "made": r}
                print(f"[보고서] 자동 보관: {r}", flush=True)
        except Exception as e:
            state["error"] = str(e)[:300]
            traceback.print_exc()
        time.sleep(600)


def start() -> None:
    if AUTO and not state.get("thread"):
        state["thread"] = True
        threading.Thread(target=_loop, name="report-auto", daemon=True).start()
