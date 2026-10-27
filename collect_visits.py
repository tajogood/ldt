#!/usr/bin/env python3
"""KAHIS 축산차량 시설방문 정보 — 웹 서버와 따로 돌리는 수집기.

    ./visits.sh                 계속 실행: 최근 3년치를 최근 날짜부터 채우고, 다 받으면 매일 어제·오늘분 갱신
    ./visits.sh --once          빠진 날을 한 번 다 받고 끝냄(매일 갱신은 안 함)
    ./visits.sh --day 2026-10-25            그 날만 받기(이미 있어도 다시 받아 덮어씀)
    ./visits.sh --from 2026-10-01 --to 2026-10-20   기간 받기(최근 날짜부터)
    ./visits.sh --probe         이 컴퓨터에서 조회가 되는지 접속 방식별로 확인하고 끝냄
    ./visits.sh --status        받은 날짜·건수만 보여 주고 끝냄

옵션: --years 3 (과거 범위), --pause 1 (시도 사이 초), --day-pause 1 (날짜 사이 초)
데이터는 웹 서버와 같은 DB(.env 의 DATABASE_URL)의 vehicle_visit / visit_place / visit_kind / vehicle_visit_day 에 들어간다.
Ctrl+C 로 멈추면 그때 받던 날만 버리고, 다음 실행 때 이어서 받는다.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from datetime import date, datetime, timedelta

from app import db, kahis_visit as kv

log = logging.getLogger("visits")
_stop = False


def _on_signal(*_):
    global _stop
    if _stop:
        sys.exit(1)
    _stop = True
    log.info("멈추는 중… (지금 받던 날은 저장하지 않음, 한 번 더 누르면 바로 종료)")


def _range(a: date, b: date) -> list[str]:
    """b → a 순(최근부터)."""
    out, d = [], b
    while d >= a:
        out.append(d.isoformat())
        d -= timedelta(days=1)
    return out


def _have() -> set[str]:
    with db.session() as con:
        return {str(r["date"])[:10] for r in con.rows("SELECT date FROM vehicle_visit_day")}


def collect_days(days: list[str], day_pause: float) -> tuple[int, int]:
    """날짜 목록을 차례로 받는다. 실패하면 세션을 새로 열어 같은 날을 다시 시도(점점 길게 쉼)."""
    done = visits = fails = 0
    i = 0
    with kv.client() as c:
        codes = []
        while not codes and not _stop:          # 처음 세션 열기도 실패하면 기다렸다 다시
            try:
                codes = kv.open_session(c)
            except Exception as e:
                fails += 1
                wait = min(1800, 30 * 2 ** min(fails, 6))
                log.warning("조회 화면 열기 실패(%d회째): %s → %d초 뒤 다시", fails, str(e)[:200], wait)
                for _ in range(wait):
                    if _stop:
                        break
                    time.sleep(1)
        if not codes:
            return done, visits
        fails = 0
        log.info("시도 %d곳: %s", len(codes), ",".join(codes))
        while i < len(days) and not _stop:
            d = days[i]
            t0 = time.time()
            try:
                n = kv.collect_day(c, d, codes)
            except Exception as e:  # 네트워크·세션 문제: 기다렸다 같은 날 다시
                fails += 1
                wait = min(1800, 30 * 2 ** min(fails, 6))
                log.warning("%s 실패(%d회째): %s → %d초 뒤 다시", d, fails, str(e)[:200], wait)
                kv._log(db.now(), False, 0, f"{d} 실패: {str(e)[:300]}")
                for _ in range(wait):
                    if _stop:
                        break
                    time.sleep(1)
                try:
                    codes = kv.open_session(c)
                except Exception as e2:
                    log.warning("세션 다시 열기 실패: %s", str(e2)[:200])
                continue
            fails = 0
            done += 1
            visits += n
            i += 1
            log.info("%s  방문 %7s건  %3d초  (%d/%d)", d, f"{n:,}", round(time.time() - t0), i, len(days))
            if i % 30 == 0:
                codes = kv.open_session(c)      # 세션을 주기적으로 새로
            time.sleep(day_pause)
    return done, visits


def status() -> dict:
    st = kv.start_date()
    with db.session() as con:
        row = con.rows("SELECT COUNT(*) AS days, MIN(date) AS lo, MAX(date) AS hi, COALESCE(SUM(visits),0) AS v "
                       "FROM vehicle_visit_day")[0]
        inside = con.scalar("SELECT COUNT(*) FROM vehicle_visit_day WHERE date >= ?", [st])
        rows = con.scalar("SELECT COUNT(*) FROM vehicle_visit")
    total = (date.today() - st).days + 1
    return {"범위": f"{st} ~ {date.today()}", "받은 날": f"{inside}/{total} ({100 * inside // max(1, total)}%)",
            "전체 받은 날": row["days"], "기간": f"{row['lo']} ~ {row['hi']}", "방문 합계": f"{row['v']:,}",
            "저장 행": f"{rows:,}"}


def main() -> None:
    ap = argparse.ArgumentParser(description="KAHIS 축산차량 시설방문 수집기")
    ap.add_argument("--once", action="store_true", help="빠진 날을 한 번 받고 끝냄")
    ap.add_argument("--day", help="그 날만 받기(YYYY-MM-DD)")
    ap.add_argument("--from", dest="date_from", help="기간 시작(YYYY-MM-DD)")
    ap.add_argument("--to", dest="date_to", help="기간 끝(YYYY-MM-DD, 기본 오늘)")
    ap.add_argument("--years", type=int, default=None, help="과거 범위(년, 기본 3)")
    ap.add_argument("--pause", type=float, default=None, help="시도 사이 쉬는 초(기본 1)")
    ap.add_argument("--day-pause", type=float, default=None, help="날짜 사이 쉬는 초(기본 1)")
    ap.add_argument("--daily-at", default="04:30", help="매일 갱신 시각(기본 04:30)")
    ap.add_argument("--probe", action="store_true", help="접속 확인만")
    ap.add_argument("--status", action="store_true", help="수집 현황만")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%m-%d %H:%M:%S")
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    db.init()
    if a.years:
        kv.YEARS = a.years
    if a.pause is not None:
        kv.PAUSE = a.pause
    day_pause = kv.DAY_PAUSE if a.day_pause is None else a.day_pause

    if a.status:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return
    if a.probe:
        print(json.dumps(kv.probe(), ensure_ascii=False, indent=2))
        return
    if a.day:
        print(collect_days([a.day], day_pause))
        return
    if a.date_from:
        lo = date.fromisoformat(a.date_from)
        hi = date.fromisoformat(a.date_to) if a.date_to else date.today()
        print(collect_days(_range(lo, hi), day_pause))
        return

    log.info("축산차량 방문 수집 시작 — %s 까지 과거분, 시도 사이 %ss, 날짜 사이 %ss", kv.start_date(), kv.PAUSE, day_pause)
    log.info("현황 %s", status())
    last_daily = None
    while not _stop:
        # 1) 어제·오늘: 하루 한 번(시작할 때와 매일 정해진 시각 이후)
        now = datetime.now()
        h, m = (int(x) for x in a.daily_at.split(":"))
        due = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if last_daily is None or (now >= due and last_daily < due):
            today = date.today()
            d, n = collect_days([today.isoformat(), (today - timedelta(days=1)).isoformat()], day_pause)
            kv._log(db.now(), True, n, f"매일 갱신 {d}일 · 방문 {n:,}건")
            last_daily = datetime.now()
        # 2) 과거분: 빠진 날을 최근부터
        have = _have()
        todo = [d for d in _range(kv.start_date(), date.today() - timedelta(days=2)) if d not in have]
        if todo and not _stop:
            log.info("과거분 남은 날 %d일 (%s ~ %s)", len(todo), todo[-1], todo[0])
            # 매일 갱신 시각을 놓치지 않도록 하루치(약 100일)씩 끊어서
            d, n = collect_days(todo[:100], day_pause)
            kv._log(db.now(), True, n, f"과거분 {d}일 · 방문 {n:,}건")
            continue
        if a.once:
            break
        # 3) 다 받았으면 다음 매일 갱신까지 대기
        nxt = due if datetime.now() < due else due + timedelta(days=1)
        log.info("과거분 다 받음. 다음 갱신 %s", nxt.strftime("%m-%d %H:%M"))
        while not _stop and datetime.now() < nxt:
            time.sleep(30)
    log.info("끝. 현황 %s", status())


if __name__ == "__main__":
    main()
