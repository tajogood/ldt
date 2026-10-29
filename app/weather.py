"""과거 기온: 발생 시기만 골라서 받기.

연관 분석(기온과 발생)은 '발생이 있는 달'(가금이면 대개 10~4월)의 기온만 쓴다. 그래서 과거 전체가 아니라
최근 12개 시즌의 그 달들(+ 직전 14일)만, 관측소마다 시즌당 한 번 요청해서 받는다(요청 1회 = 최대 999일).
이미 받아 둔 기간(80% 이상 채워짐)은 건너뛴다. 현재·앞으로의 기온은 매일 수집(kma_asos_daily)이 채운다.

요청 수 ≈ 관측소 57 × 시즌 12 ≈ 700회(처음 한 번), 1회당 1초 쉬면 15분 안팎.
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from collections import Counter
from datetime import date, timedelta

from . import analysis as A
from . import collect, db
from .settings import DATA_GO_KR_KEY
from .species import SPECIES

SEASONS = 12
PAUSE = float(os.environ.get("WEATHER_PAUSE_SEC", "1"))
ENABLED = os.environ.get("WEATHER_BACKFILL", "1") == "1"
state: dict = {"running": False, "done": 0, "total": 0, "rows": 0, "error": None, "finished_at": None, "ranges": []}


def _active_months(species: str) -> list[int]:
    with db.session() as con:
        ds = [A._d(str(r["date"])) for r in con.rows("SELECT date FROM event WHERE species = ? AND kind = 'outbreak'", [species])]
    c = Counter(d.month for d in ds)
    return sorted(m for m, n in c.items() if ds and n / len(ds) >= 0.02)


def needed_ranges() -> list[tuple[date, date]]:
    """축종별 발생 달 × 최근 12개 시즌을 날짜 구간으로 만들고 겹치면 합친다(직전 14일 포함)."""
    yday = date.today() - timedelta(days=1)
    out = []
    for sp, cfg in SPECIES.items():
        months = set(_active_months(sp))
        if not months:
            continue
        sm = cfg["season_start_month"]
        order = [((sm - 1 + i) % 12) + 1 for i in range(12)]            # 시즌 안 달 순서
        first = next(i for i, m in enumerate(order) if m in months)
        last = max(i for i, m in enumerate(order) if m in months)
        cur = A.season_of(date.today(), sm)
        for y in range(cur - SEASONS + 1, cur + 1):
            def month_start(i):
                yy = y + (1 if sm + i > 12 else 0)
                return date(yy, order[i], 1)
            a = month_start(first) - timedelta(days=14)
            nb = month_start(last)
            b = (date(nb.year + (nb.month == 12), nb.month % 12 + 1, 1) - timedelta(days=1))
            if a > yday:
                continue
            out.append((a, min(b, yday)))
    out.sort()
    merged: list[list[date]] = []
    for a, b in out:
        if merged and a <= merged[-1][1] + timedelta(days=1):
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    # 한 요청이 999일을 넘지 않게 자른다
    res = []
    for a, b in merged:
        while a <= b:
            e = min(b, a + timedelta(days=900))
            res.append((a, e))
            a = e + timedelta(days=1)
    return res


def plan() -> list[tuple[str, str, date, date]]:
    src = collect.get_source("kma_asos_daily")
    tasks = []
    for a, b in needed_ranges():
        days = (b - a).days + 1
        with db.session() as con:
            have = {str(r["place"]).split(":")[0]: r["n"] for r in con.rows(
                "SELECT place, COUNT(*) AS n FROM context_ts WHERE source = 'kma_asos_daily' AND metric = 'temp_avg' "
                "AND date BETWEEN ? AND ? GROUP BY place", [a, b])}
        for stn, name in src["stations"].items():
            if have.get(str(stn), 0) < days * 0.8:
                tasks.append((str(stn), name, a, b))
    return tasks


def run(pause: float = PAUSE) -> dict:
    src = collect.get_source("kma_asos_daily")
    tasks = plan()
    state.update(running=True, done=0, total=len(tasks), rows=0, error=None,
                 ranges=[f"{a}~{b}" for a, b in needed_ranges()])
    try:
        for stn, name, a, b in tasks:
            s = {**src, "params": {"dataType": "JSON", "dataCd": "ASOS", "dateCd": "DAY",
                                   "startDt": f"{a:%Y%m%d}", "endDt": f"{b:%Y%m%d}", "stnIds": stn},
                 "paging": {"page": "pageNo", "size": "numOfRows", "per": 999}}
            rows = []
            for attempt in range(3):
                try:
                    for rec in collect.fetch_paged(s, {"serviceKey": DATA_GO_KR_KEY}):
                        rec["_stn_name"] = name
                        rows.extend(collect._asos_rows(rec))
                    break
                except Exception as e:      # 일시 오류는 잠깐 쉬었다 다시
                    state["error"] = f"{stn} {a}~{b}: {str(e)[:160]}"
                    time.sleep(5 * (attempt + 1))
            if rows:
                with db.session() as con:
                    state["rows"] += db.upsert(con, "context_ts", rows)
            state["done"] += 1
            time.sleep(pause)
    except Exception as e:
        traceback.print_exc()
        state["error"] = str(e)[:300]
    finally:
        state.update(running=False, finished_at=db.now().isoformat(timespec="seconds"))
    return status()


def _loop() -> None:
    time.sleep(60)                 # 서버가 뜬 뒤 다른 수집과 겹치지 않게 잠깐 쉬었다가
    while True:
        try:
            if plan():
                run()
        except Exception:
            traceback.print_exc()
        time.sleep(24 * 3600)      # 새 시즌이 생기면 다음 날 그 구간만 더 받음


def start() -> None:
    if ENABLED and DATA_GO_KR_KEY and not state.get("thread"):
        state["thread"] = True
        threading.Thread(target=_loop, name="weather-backfill", daemon=True).start()


def status() -> dict:
    return {k: v for k, v in state.items() if k != "thread"}
