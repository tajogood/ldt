"""KAHIS 축산차량 시설방문 정보 (농림축산검역본부, 공공데이터 LINK형 공개 조회 화면).

공공데이터포털 '축산차량 방문정보'(LINK: home.kahis.go.kr/home/main/visitFrmhsListPopup.do)의 조회 결과를
날짜·시도 단위로 받아, (날짜, 리 단위 소재지, 시설구분, 축종, 차량유형)별 방문 횟수로 묶어 저장한다.
시설명·차량번호는 원래 화면에서 가려져 있어 저장하지 않는다(받지도 못함).

조회는 화면과 같은 순서로 한다: 조회 화면 열기(세션) → 지역 목록 조회(시도) → 목록 조회.
목록 조회만 바로 보내면 서버가 'ACCESS가 거부'로 막는다.

- 매일(자동 수집 04:30): 어제·오늘(오늘은 부분값, 다음 날 다시 받아 덮어씀)
- 과거 채우기: 서버가 켜져 있는 동안 별도 스레드가 최근 날짜부터 VISIT_YEARS(기본 3)년 전까지
  하루씩 거슬러 올라가며 받는다(최근 날짜부터 3년 전까지). 다 받으면 매일 갱신만 한다. 받은 날은 vehicle_visit_day 에 남아 재시작해도 이어서 한다.
공공기관 서버에 부담을 주지 않도록 시도 하나씩 차례로, 요청·날짜 사이에 쉰다.
"""
from __future__ import annotations

import html
import os
import re
import threading
import time
import traceback
from collections import Counter
from datetime import date, timedelta

import httpx

from . import db
from .species import species_code

BASE = "https://home.kahis.go.kr"
FORM = BASE + "/home/main/visitFrmhsListPopup.do"
AREA = BASE + "/home/common/selectLegaldongList.do"
LIST = BASE + "/home/main/selectVisitFrmhsList.do"
SID = "kahis_vehicle_visit"
# 하루 전국 약 17만 건(원본 약 140MB, 저장 약 4~5MB). 과거분은 최근 3년(VISIT_YEARS)까지만 받고, 그 뒤로는 매일 갱신.
YEARS = int(os.environ.get("VISIT_YEARS", "3"))
START_ENV = os.environ.get("VISIT_START")      # 날짜를 직접 지정하면 그 날짜까지


def start_date() -> date:
    if START_ENV:
        return date.fromisoformat(START_ENV)
    t = date.today()
    return t.replace(year=t.year - YEARS) if not (t.month == 2 and t.day == 29) else t - timedelta(days=365 * YEARS)
PAUSE = float(os.environ.get("VISIT_PAUSE_SEC", "1"))          # 시도 요청 사이
DAY_PAUSE = float(os.environ.get("VISIT_DAY_PAUSE_SEC", "1"))     # 과거 채우기에서 날짜 사이
# 과거분 수집은 별도 수집기(./visits.sh)가 맡는다. 웹 서버 안에서 돌리려면 VISIT_BACKFILL=1
BACKFILL = os.environ.get("VISIT_BACKFILL", "0") == "1"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36")
BASE_HEADERS = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"}
POST_HEADERS = {**BASE_HEADERS, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": BASE, "Referer": FORM}

_CELL = re.compile(r'class="list_g_line"[^>]*>(.*?)</td>', re.S)
_DENIED = "ACCESS가 거부"
state: dict = {"backfill": None}


class Denied(RuntimeError):
    pass


# ── 세션·조회 ───────────────────────────────────────────────────────────
def open_session(client: httpx.Client) -> list[str]:
    """조회 화면을 열고 지역 목록을 한 번 불러 세션을 만든다. 시도 코드 목록을 돌려준다."""
    r = client.get(FORM, headers={**BASE_HEADERS, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    if r.status_code != 200 or _DENIED in r.text:
        raise Denied(f"조회 화면 열기 실패(HTTP {r.status_code})")
    sel = r.text[r.text.find('id="ctprvn"'):]
    codes = re.findall(r'value="(\d{2})"', sel[:sel.find("</select>")])
    if not codes:
        raise RuntimeError("조회 화면에서 시도 목록을 찾지 못했습니다(화면 구조 변경 가능)")
    a = client.post(AREA, data={"ctprvn": codes[0], "signgu": "", "emd": "", "li": "", "flag": "li"},
                    headers={**POST_HEADERS, "X-Requested-With": "XMLHttpRequest", "Accept": "*/*"})
    if a.status_code != 200:
        raise Denied(f"지역 목록 조회 실패(HTTP {a.status_code})")
    return codes


def parse(page: str) -> list[tuple]:
    """결과 표 → (방문일시, 소재지, 시설구분, 축종, 차량유형) 목록. 7칸이 한 행."""
    cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", c))).strip() for c in _CELL.findall(page)]
    rows = []
    for i in range(0, len(cells) - 6, 7):
        at, addr, fac, _name, sp, vt, _no = cells[i:i + 7]
        if re.match(r"\d{4}-\d{2}-\d{2}", at):
            rows.append((at, addr, fac, sp, vt))
    return rows


def fetch(client: httpx.Client, day: str, sido: str) -> list[tuple]:
    data = {"visitDt": day, "ctprvn": sido, "signgu": " ", "emd": " ", "li": " "}
    for attempt in range(3):
        try:
            r = client.post(LIST, data=data, headers={**POST_HEADERS, "Accept": "text/html,*/*;q=0.8"}, timeout=240)
        except httpx.TransportError:
            if attempt == 2:
                raise
            time.sleep(10 * (attempt + 1))
            continue
        if _DENIED in r.text or r.status_code == 404:
            if attempt == 2:
                raise Denied(f"목록 조회 거부(HTTP {r.status_code}, 쿠키: {', '.join(client.cookies.keys()) or '없음'})")
            open_session(client)          # 세션이 끊겼으면 화면부터 다시 열고 재시도
            time.sleep(3)
            continue
        r.raise_for_status()
        return parse(r.text)
    return []


def client() -> httpx.Client:
    return httpx.Client(timeout=240, follow_redirects=True, http2=False)


# ── 집계·저장 ───────────────────────────────────────────────────────────
def aggregate(day: str, rows: list[tuple]) -> list[dict]:
    """(소재지, 시설구분, 축종, 차량유형)별 횟수와 첫·마지막 방문 시각(시)."""
    cnt: Counter = Counter()
    hours: dict = {}
    for at, addr, fac, sp, vt in rows:
        k = (addr[:150], fac[:40], sp[:40], vt[:40])
        cnt[k] += 1
        h = int(at[11:13] or 0)
        lo, hi = hours.get(k, (h, h))
        hours[k] = (min(lo, h), max(hi, h))
    return [{"date": day, "address": a, "facility": f, "species_raw": sp, "vehicle": v, "n": n,
             "first_hour": hours[(a, f, sp, v)][0], "last_hour": hours[(a, f, sp, v)][1]}
            for (a, f, sp, v), n in cnt.items()]


def _split(addr: str) -> dict:
    parts = addr.split()
    return {"sido": parts[0] if parts else "",
            "sigungu": " ".join(parts[1:-2]) if len(parts) > 3 else (parts[1] if len(parts) > 1 else ""),
            "emd": parts[-2] if len(parts) >= 3 else "", "li": parts[-1] if len(parts) >= 4 else ""}


_ids: dict = {"place": {}, "kind": {}}


def _ensure_ids(con: db.DB, agg: list[dict]) -> None:
    """리 주소·방문 종류 번호표를 채우고 메모리에 기억한다."""
    if not _ids["place"]:
        _ids["place"] = {r["address"]: r["id"] for r in con.rows("SELECT id, address FROM visit_place")}
        _ids["kind"] = {(r["facility"], r["species_raw"], r["vehicle"]): r["id"]
                        for r in con.rows("SELECT id, facility, species_raw, vehicle FROM visit_kind")}
    newp = {r["address"] for r in agg} - set(_ids["place"])
    newk = {(r["facility"], r["species_raw"], r["vehicle"]) for r in agg} - set(_ids["kind"])
    if newp:
        db.upsert(con, "visit_place", [{"address": a, **_split(a)} for a in newp])
        _ids["place"].update({r["address"]: r["id"] for r in con.rows("SELECT id, address FROM visit_place")})
    if newk:
        db.upsert(con, "visit_kind", [{"facility": f, "species_raw": sp, "species": species_code(sp) or "", "vehicle": v}
                                      for f, sp, v in newk])
        _ids["kind"].update({(r["facility"], r["species_raw"], r["vehicle"]): r["id"]
                             for r in con.rows("SELECT id, facility, species_raw, vehicle FROM visit_kind")})


_save_lock = threading.Lock()   # 매일 수집과 과거 채우기가 동시에 번호표를 쓰지 않도록


def save_day(day: str, rows: list[tuple]) -> int:
    agg = aggregate(day, rows)
    with _save_lock, db.session() as con:
        _ensure_ids(con, agg)
        con.execute("DELETE FROM vehicle_visit WHERE date = ?", [date.fromisoformat(day)])
        db.upsert(con, "vehicle_visit", [{"date": day, "place_id": _ids["place"][r["address"]],
                                          "kind_id": _ids["kind"][(r["facility"], r["species_raw"], r["vehicle"])],
                                          "n": r["n"], "first_hour": r["first_hour"], "last_hour": r["last_hour"]}
                                         for r in agg])
        db.upsert(con, "vehicle_visit_day", [{"date": day, "visits": len(rows), "groups": len(agg), "at": db.now()}])
    _ids["saved"] = _ids.get("saved", 0) + 1
    if db.IS_PG and _ids["saved"] % 10 == 1:     # 큰 표의 통계를 새로(조회 계획이 느려지지 않게)
        with db.engine.connect() as c2:
            c2.execution_options(isolation_level="AUTOCOMMIT").exec_driver_sql("ANALYZE vehicle_visit")
    return len(agg)


def collect_day(c: httpx.Client, day: str, codes: list[str]) -> int:
    """하루치를 시도별로 받아 그날 집계를 통째로 바꾼다(시도 하나라도 실패하면 저장하지 않음)."""
    allrows: list[tuple] = []
    for code in codes:
        allrows += fetch(c, day, code)
        time.sleep(PAUSE)
    save_day(day, allrows)
    return len(allrows)


def _log(started, ok: bool, rows: int, msg: str) -> None:
    with db.session() as con:
        con.con.execute(db.collect_log.insert().values(source=SID, started_at=started, finished_at=db.now(),
                                                       ok=ok, rows=rows, message=msg[:500]))


def run(days: list[str] | None = None) -> dict:
    """매일 수집: 어제·오늘(또는 지정한 날짜들)."""
    started = db.now()
    today = date.today()
    days = days or [(today - timedelta(days=1)).isoformat(), today.isoformat()]
    done, visits = [], 0
    try:
        with client() as c:
            codes = open_session(c)
            for d in days:
                visits += collect_day(c, d, codes)
                done.append(d)
        _log(started, True, visits, f"{len(done)}일 · 방문 {visits:,}건 ({', '.join(done)})")
        return {"days": done, "visits": visits}
    except Exception as e:
        _log(started, False, visits, f"{len(done)}일 받은 뒤 오류: {str(e)[:300]}")
        raise


# ── 과거 채우기(백그라운드) ──────────────────────────────────────────────
def _missing_days() -> list[str]:
    """받지 않은 날짜를 최근 → 과거 순으로."""
    with db.session() as con:
        have = {str(r["date"])[:10] for r in con.rows("SELECT date FROM vehicle_visit_day")}
    d, end, out = date.today() - timedelta(days=2), start_date(), []
    while d >= end:
        s = d.isoformat()
        if s not in have:
            out.append(s)
        d -= timedelta(days=1)
    return out


def backfill_status() -> dict:
    with db.session() as con:
        n = con.scalar("SELECT COUNT(*) FROM vehicle_visit_day")
        lo = con.scalar("SELECT MIN(date) FROM vehicle_visit_day")
        hi = con.scalar("SELECT MAX(date) FROM vehicle_visit_day")
        v = con.scalar("SELECT COALESCE(SUM(visits),0) FROM vehicle_visit_day")
    st = start_date()
    total = (date.today() - st).days + 1
    with db.session() as con:
        n = con.scalar("SELECT COUNT(*) FROM vehicle_visit_day WHERE date >= ?", [st])
    return {"start": st.isoformat(), "days_done": n, "days_total": total, "from": str(lo)[:10] if lo else None,
            "to": str(hi)[:10] if hi else None, "visits": v, **(state.get("backfill") or {})}


def _backfill_loop() -> None:
    time.sleep(20)                      # 서버가 먼저 뜨도록
    fails = 0
    while True:
        todo = _missing_days()
        if not todo:
            state["backfill"] = {"running": False, "note": "다 받음"}
            time.sleep(6 * 3600)
            continue
        try:
            with client() as c:
                codes = open_session(c)
                for i, d in enumerate(todo):
                    state["backfill"] = {"running": True, "current": d, "left": len(todo) - i}
                    t0 = time.time()
                    n = collect_day(c, d, codes)
                    state["backfill"].update(last=d, last_visits=n, last_sec=round(time.time() - t0))
                    fails = 0
                    time.sleep(DAY_PAUSE)
                    if i and i % 30 == 0:
                        codes = open_session(c)      # 세션을 주기적으로 새로
        except Exception as e:
            fails += 1
            wait = min(3600, 120 * fails)
            state["backfill"] = {"running": False, "error": str(e)[:300], "retry_in_sec": wait}
            _log(db.now(), False, 0, f"과거 채우기 오류(재시도 {wait}초 후): {str(e)[:300]}")
            traceback.print_exc()
            time.sleep(wait)


def start_backfill() -> None:
    if BACKFILL and not state.get("thread"):
        state["thread"] = True
        threading.Thread(target=_backfill_loop, name="visit-backfill", daemon=True).start()


def probe(day: str | None = None, sido: str = "36") -> dict:
    """접속 방식별로 조회가 되는지 확인(문제 진단용). 쿠키 값은 돌려주지 않는다."""
    day = day or (date.today() - timedelta(days=1)).isoformat()
    data = {"visitDt": day, "ctprvn": sido, "signgu": " ", "emd": " ", "li": " "}
    out = {}

    def step(c, name, fn):
        try:
            r = fn()
            out.setdefault(name, []).append({"status": r.status_code, "denied": _DENIED in r.text, "bytes": len(r.content),
                                             "rows": len(parse(r.text)), "set_cookie": [h.split("=")[0] for h in
                                                                                         r.headers.get_list("set-cookie")],
                                             "cookies": list(c.cookies.keys())})
        except Exception as e:
            out.setdefault(name, []).append({"error": str(e)[:200]})

    variants = {
        "form_only": lambda c: [c.get(FORM, headers=BASE_HEADERS)],
        "form_area": lambda c: [c.get(FORM, headers=BASE_HEADERS),
                                c.post(AREA, data={"ctprvn": sido, "signgu": "", "emd": "", "li": "", "flag": "li"},
                                       headers={**POST_HEADERS, "X-Requested-With": "XMLHttpRequest"})],
        "home_form_area": lambda c: [c.get(BASE + "/", headers=BASE_HEADERS), c.get(FORM, headers=BASE_HEADERS),
                                     c.post(AREA, data={"ctprvn": sido, "signgu": "", "emd": "", "li": "", "flag": "li"},
                                            headers={**POST_HEADERS, "X-Requested-With": "XMLHttpRequest"})],
    }
    for name, pre in variants.items():
        with client() as c:
            for r in pre(c):
                out.setdefault(name, []).append({"pre": r.request.url.path, "status": r.status_code,
                                                 "denied": _DENIED in r.text,
                                                 "set_cookie": [h.split("=")[0] for h in r.headers.get_list("set-cookie")]})
            step(c, name, lambda: c.post(LIST, data=data, headers=POST_HEADERS))
    return {"day": day, "sido": sido, "variants": out}
