"""자동 수집 — 서버(./run.sh)가 켜져 있는 동안 백그라운드에서 돈다.

- 처음 켰을 때 핵심 데이터가 비어 있으면: 전체 수집(초기 적재)
- 매일 AUTO_COLLECT_AT(기본 04:30, 서버 시각)에 한 번: 최신화 수집
  · 월별 반복 원천은 최근 3개월, 연도별은 최근 2년만 다시 받는다(과거분은 이미 DB에 있음)
  · 그 시각에 맥이 잠들어 있었으면, 깨어난 뒤 첫 확인 때 그날 몫을 바로 실행
- 실행 기록은 collect_log 의 source='auto:initial' / 'auto:daily' 로 남아 화면에 표시된다.
"""
from __future__ import annotations

import copy
import os
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone

from . import collect, db

ENABLED = os.environ.get("AUTO_COLLECT", "1") == "1"
AT = os.environ.get("AUTO_COLLECT_AT", "04:30")
GROUPS = [g.strip() for g in os.environ.get("AUTO_COLLECT_GROUPS", "core").split(",") if g.strip()]
GEOCODE_LIMIT = int(os.environ.get("GEOCODE_LIMIT", "3000"))
CHECK_EVERY = 300  # 초

_lock = threading.Lock()
state = {"running": None, "started_at": None, "last": None, "next": None, "enabled": ENABLED, "at": AT, "groups": GROUPS}


def _targets() -> list[dict]:
    """자동 수집 대상: 지정 그룹 + 그룹이 없는 설정 완료 원천(기상청, 행안부, 수기 파일 등)."""
    return [s for s in collect.load_sources()
            if collect.is_configured(s) and (s.get("group") in GROUPS or not s.get("group"))]


def _recent(src: dict) -> dict:
    """최신화용: 반복 범위를 최근 구간으로 줄인 사본."""
    inc = src.get("incremental")
    if inc:
        # 갱신시점 조건을 지원하는 원천: 마지막 성공 이후(겹침 며칠) 바뀐 것만
        with db.session() as con:
            last = con.scalar("SELECT MAX(started_at) FROM collect_log WHERE source = ? AND ok", [src["id"]])
        if last:
            t = datetime.fromisoformat(str(last)) - timedelta(days=inc.get("overlap_days", 3))
            s = copy.deepcopy(src)
            s["_recent"] = True
            s["params"] = {k: v for k, v in (s.get("params") or {}).items() if k not in inc.get("drop", [])}
            s["params"][inc["param"]] = t.strftime(inc.get("fmt", "%Y%m%d"))
            return s
        return src
    it = src.get("iterate")
    if not it:
        return src          # 반복 없는 원천(현황 스냅샷)은 매번 전체를 다시 받는다
    s = copy.deepcopy(src)
    s["_recent"] = True
    today = date.today()
    if it.get("kind") == "month":
        d = (today.replace(day=1) - timedelta(days=62)).replace(day=1)   # 이번 달 포함 최근 3개월
        s["iterate"]["from"] = f"{d:%Y-%m}"
    elif it.get("kind") == "year":
        s["iterate"]["from"] = today.year - 1
    return s


def _log(source: str, started: datetime, ok: bool, rows: int, msg: str) -> None:
    with db.session() as con:
        con.con.execute(db.collect_log.insert().values(source=source, started_at=started, finished_at=db.now(),
                                                       ok=ok, rows=rows, message=msg[:500]))


def _pending() -> list[dict]:
    """한 번도 성공한 적 없고, 최근 12시간 안에 시도도 안 한 원천(업데이트로 지워졌거나 새로 추가된 원천)."""
    cut = db.now() - timedelta(hours=12)
    out = []
    for s in _targets():
        if _ever_ok(s["id"]):
            continue
        with db.session() as con:
            last = con.scalar("SELECT MAX(finished_at) FROM collect_log WHERE source = ?", [s["id"]])
        if last:
            t = datetime.fromisoformat(str(last))
            if (t if t.tzinfo else t.replace(tzinfo=timezone.utc)) > cut:
                continue
        out.append(s)
    return out


def run_batch(kind: str, only: list[dict] | None = None) -> dict:
    """kind: initial(전체) | daily(최신화) | fill(빈 원천만 채움). 동시에 두 번 돌지 않는다."""
    if not _lock.acquire(blocking=False):
        return {"skipped": "already running"}
    started = db.now()
    state.update(running=kind, started_at=started.isoformat(timespec="seconds"))
    done, failed, rows = [], [], 0
    try:
        targets = only if only is not None else _targets()
        # 발생 정보를 먼저 받아 바로 좌표를 붙인다(지도 표시가 가장 급함)
        targets.sort(key=lambda s: s["id"] != "mafra_disease_outbreak")
        for src in targets:
            state["current"] = src["id"]
            # 한 번도 성공한 적 없는 원천은 전체, 이미 있는 원천은 최근 구간만 (중단 후 재시작해도 이어서 채움)
            full = kind == "initial" or not _ever_ok(src["id"])
            s = src if full else _recent(src)
            try:
                n = collect.run_source(s)
                rows += n
                done.append(src["id"])
            except Exception as e:  # 원천 하나가 실패해도 나머지는 계속
                failed.append(f"{src['id']}: {str(e)[:120]}")
            if src.get("geocode") or src["provider"] == "kahis_visit":
                _geocode(failed)
            if src["id"] == "mafra_farm_by_area":
                _regions(failed)
        _geocode(failed)
        _regions(failed)
        msg = f"원천 {len(done)}개 · {rows}행" + (f" · 실패 {len(failed)}: " + " / ".join(failed) if failed else "")
        _log(f"auto:{kind}", started, not failed, rows, msg)
        state["last"] = {"kind": kind, "finished_at": db.now().isoformat(timespec="seconds"), "ok": not failed, "message": msg}
        return state["last"]
    finally:
        state.update(running=None, started_at=None, current=None)
        _lock.release()


def _ever_ok(sid: str) -> bool:
    with db.session() as con:
        return bool(con.scalar("SELECT COUNT(*) FROM collect_log WHERE source = ? AND ok", [sid]))


def _geocode(failed: list) -> None:
    from . import geocode
    if not geocode.VWORLD_KEY:
        return
    state["current"] = "geocode"
    try:
        r = geocode.run(GEOCODE_LIMIT)
        state["geocode_last"] = r
    except Exception as e:
        failed.append(f"geocode: {str(e)[:160]}")
        state["geocode_last"] = {"error": str(e)[:300]}


def _regions(failed: list) -> None:
    """읍면동·시군 대표점(농장 분포 지도·위험지수용). 이미 찾은 곳은 건너뛰므로 두 번째부터는 금방 끝난다."""
    from . import geocode
    if not geocode.VWORLD_KEY:
        return
    state["current"] = "geocode:regions"
    try:
        state["regions_last"] = geocode.regions()
    except Exception as e:
        failed.append(f"regions: {str(e)[:160]}")
    state["current"] = "geocode:shapes"
    try:
        state["shapes_last"] = geocode.shapes()     # 읍면동 경계(처음 한 번 수 분, 이후 180일마다)
    except Exception as e:
        failed.append(f"shapes: {str(e)[:160]}")


def _has_shapes() -> bool:
    with db.session() as con:
        return bool(con.scalar("SELECT COUNT(*) FROM region_shape"))


def geo_status() -> dict:
    with db.session() as con:
        return {
            "with_coords": con.scalar("SELECT COUNT(*) FROM event WHERE lat IS NOT NULL"),
            "without_coords": con.scalar("SELECT COUNT(*) FROM event WHERE lat IS NULL AND place IS NOT NULL AND place <> ''"),
            "cache_ok": con.scalar("SELECT COUNT(*) FROM geo_cache WHERE status='OK'"),
            "cache_not_found": con.scalar("SELECT COUNT(*) FROM geo_cache WHERE status='NOT_FOUND'"),
            "township_shapes": con.scalar("SELECT COUNT(*) FROM region_shape"),
        }


def _due_time(day: date) -> datetime:
    h, m = (int(x) for x in AT.split(":"))
    return datetime(day.year, day.month, day.day, h, m)


def _last_run(source: str) -> str | None:
    """성공·부분실패 상관없이 마지막으로 끝난 시각(실패한 원천 때문에 하루 종일 재시도하지 않도록)."""
    with db.session() as con:
        return con.scalar("SELECT MAX(finished_at) FROM collect_log WHERE source = ?", [source])


def _needs_initial() -> bool:
    with db.session() as con:
        n = con.scalar("SELECT COUNT(*) FROM event WHERE source = 'mafra_disease_outbreak'")
    return not n and not _last_run("auto:initial")


def _loop() -> None:
    time.sleep(5)  # 서버가 먼저 뜨도록
    try:
        if _needs_initial():
            run_batch("initial")
        else:
            todo = _pending()       # 업데이트 후 비워진 원천은 다음 날까지 기다리지 않고 바로 채운다
            if todo:
                run_batch("fill", todo)
            elif not _has_shapes():
                _regions([])
    except Exception:
        traceback.print_exc()
    while True:
        try:
            now = datetime.now()
            today_due = _due_time(now.date())
            state["next"] = (today_due if now < today_due else _due_time(now.date() + timedelta(days=1))).isoformat(timespec="minutes")
            last = max(filter(None, [_last_run("auto:daily"), _last_run("auto:initial")]), default=None)
            last_local = datetime.fromisoformat(last).astimezone().replace(tzinfo=None) if last else None
            # 오늘 정해진 시각이 지났는데 그 이후 성공한 수집이 없으면 실행(잠자기 후 따라잡기 포함)
            if now >= today_due and (last_local is None or last_local < today_due):
                run_batch("daily")
        except Exception:
            traceback.print_exc()
        time.sleep(CHECK_EVERY)


def start() -> None:
    if ENABLED:
        from . import kahis_visit
        kahis_visit.start_backfill()     # 축산차량 방문 과거분: 별도 스레드로 계속 거슬러 올라가며 받음
    if ENABLED and not state.get("thread"):
        t = threading.Thread(target=_loop, name="auto-collect", daemon=True)
        state["thread"] = True
        t.start()


def status() -> dict:
    out = {k: v for k, v in state.items() if k != "thread"}
    try:
        out["geo"] = geo_status()
        from . import kahis_visit
        out["visits"] = kahis_visit.backfill_status()
    except Exception:
        pass
    return out
