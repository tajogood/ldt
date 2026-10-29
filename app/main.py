"""FastAPI 서버 — /api/* JSON + web/ 정적 화면.

목록·분석 응답은 모두 {"items"|결과..., "freshness": [...]} 형태로,
쓰인 데이터의 원천·기간·수집시각을 함께 돌려준다(app/freshness.py).
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from fastapi.middleware.gzip import GZipMiddleware
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analysis, assoc, birds, collect, db, explore, freshness, history, news, scheduler, wild
from . import weather as weather_bf      # 과거 기온 백필(아래 /api/weather 함수 이름과 겹치지 않게)
from .settings import VWORLD_KEY, WEB_DIR
from .species import SPECIES, disease_code

app = FastAPI(title="가축질병 추적 시스템 / Livestock Disease Tracker", version="0.6.0")
app.add_middleware(GZipMiddleware, minimum_size=2000)   # 읍면동 경계 GeoJSON 등 큰 응답 압축
db.init()


_STARTED = db.now().isoformat(timespec="seconds")


def _load_wild():
    try:
        r = wild.load()
        if r["files"]:
            print("야생 검출 파일:", r)
    except Exception as e:  # 파일이 이상해도 서버는 뜬다
        print("야생 검출 파일 읽기 실패:", e)


@app.on_event("startup")
def _start_auto_collect():
    import faulthandler
    faulthandler.cancel_dump_traceback_later()   # 정상적으로 켜졌으니 '멈춤 진단' 취소
    print("앱 준비 완료", flush=True)
    scheduler.start()
    news.start()      # 뉴스 5분 · 공지 30분마다(NEWS_MONITOR=0 이면 꺼짐)
    weather_bf.start()   # 과거 발생 시기 기온만 골라 받기(연관 분석용, WEATHER_BACKFILL=0 이면 꺼짐)
    import threading
    threading.Thread(target=_load_wild, name="wild-files", daemon=True).start()   # data/files 의 야생 검출 목록
    try:              # 철새 이동 정보 파일이 있고 아직 안 넣었으면 넣기
        with db.session() as con:
            empty = not con.scalar("SELECT COUNT(*) FROM bird_track")
        if empty:
            birds.load()
    except Exception:
        pass


def _season_start() -> str:
    t = date.today()
    return date(t.year if t.month >= 9 else t.year - 1, 9, 1).isoformat()


def _bbox(s: str | None):
    if not s:
        return None
    try:
        a, b, c, d = (float(x) for x in s.split(","))
        return a, b, c, d  # lat_min, lon_min, lat_max, lon_max
    except ValueError:
        raise HTTPException(400, "bbox=lat_min,lon_min,lat_max,lon_max")


@app.get("/config.js", response_class=PlainTextResponse)
def config_js():
    cfg = {"vworldKey": VWORLD_KEY, "dialect": db.engine.dialect.name,
           "species": {k: {"label": v["label"], "label_en": v["label_en"], "diseases": v["diseases"],
                           "diseases_en": v["diseases_en"], "radii_km": v["radii_km"],
                           "main_disease": v["main_disease"], "season_start_month": v["season_start_month"],
                           "link": v["link"]} for k, v in SPECIES.items()}}
    return PlainTextResponse(f"window.LDT_CONFIG = {json.dumps(cfg, ensure_ascii=False)};",
                             media_type="application/javascript")


@app.get("/api/summary")
def summary(species: str = "poultry", since: str | None = None):
    where, params = analysis.species_filter(species)
    d21 = (date.today() - timedelta(days=21)).isoformat()
    with db.session() as con:
        latest = con.scalar(f"SELECT MAX(date) FROM event WHERE {where} AND kind IN ('outbreak','wild_detection')", params)
        if not since:
            since = _season_start()
            # 원천 데이터가 이번 동절기 전에 끊겨 있으면 마지막 1년을 보여준다(데이터 기준일은 freshness로 표시)
            if latest and latest < since:
                since = (date.fromisoformat(latest) - timedelta(days=365)).isoformat()
        kpi = {
            "season_start": since, "data_latest": latest,
            "outbreaks": con.scalar(f"SELECT COUNT(*) FROM event WHERE {where} AND kind='outbreak' AND date>=?", params + [since]),
            "wild_detections": con.scalar(f"SELECT COUNT(*) FROM event WHERE {where} AND kind='wild_detection' AND date>=?", params + [since]),
            "restrictions_active": con.scalar(f"SELECT COUNT(*) FROM event WHERE {where} AND kind='movement_restriction' "
                                              "AND (note IS NULL OR note='' OR note>=?)", params + [date.today().isoformat()]),
            "positives_21d": con.scalar(f"SELECT COUNT(*) FROM event WHERE {where} AND kind='surveillance_positive' AND date>=?", params + [d21]),
            "farms": con.scalar("SELECT COUNT(*) FROM facility WHERE kind='farm' AND lat IS NOT NULL AND (?='all' OR species=?)",
                                [species, species]),
        }
        if not kpi["farms"]:   # 농장 좌표가 없으면 농림부 읍면동 농장 통계 합계
            sw, sp = ("species = ?", [species]) if species != "all" else ("1=1", [])
            kpi["farms"] = round(con.scalar(f"SELECT COALESCE(SUM(value),0) FROM region_stat WHERE source='mafra_farm_by_area' "
                                            f"AND metric LIKE 'farms%' AND {sw}", sp) or 0)
            kpi["farms_source"] = "region_stat"
            kpi["heads"] = round(con.scalar(f"SELECT COALESCE(SUM(value),0) FROM region_stat WHERE source='mafra_farm_by_scale' "
                                            f"AND metric LIKE 'heads%' AND {sw}", sp) or 0)
        # 직전 시즌 같은 기간(시즌 시작일 ~ 오늘 날짜에 해당하는 날) — 이번 시즌 수치를 읽는 기준
        s0 = date.fromisoformat(since)
        p_from = s0.replace(year=s0.year - 1).isoformat()
        x = min(date.today(), date.fromisoformat(latest) if latest else date.today())
        x = x - timedelta(days=365) if (x.month, x.day) == (2, 29) else x.replace(year=x.year - 1)
        p_to = max(x, s0.replace(year=s0.year - 1)).isoformat()
        kpi["prev"] = {
            "from": p_from, "to": p_to,
            "outbreaks": con.scalar(f"SELECT COUNT(*) FROM event WHERE {where} AND kind='outbreak' AND date BETWEEN ? AND ?",
                                    params + [p_from, p_to]),
            "wild_detections": con.scalar(f"SELECT COUNT(*) FROM event WHERE {where} AND kind='wild_detection' AND date BETWEEN ? AND ?",
                                          params + [p_from, p_to]),
        }
        alert = con.rows(f"SELECT date, note FROM event WHERE {where} AND kind='alert' ORDER BY date DESC LIMIT 1", params)
        feed = con.rows(f"SELECT id, kind, disease, species, subspecies, date, sido, sigungu, place, note FROM event "
                        f"WHERE {where} ORDER BY date DESC, id DESC LIMIT 12", params)
        fr = freshness.merge(
            freshness.of(con, "event", where, params),
            freshness.of(con, "facility", "kind='farm' AND (?='all' OR species=?)", [species, species], date_col=None))
    return {"kpi": kpi, "alert": alert[0] if alert else None, "feed": feed, "freshness": fr}


@app.get("/api/farms/density")
def farms_density(species: str = "poultry", level: str = "emd"):
    """농림부 읍면동 농장 통계 + 행정구역 대표점 → 농장 분포 지도."""
    res = analysis.farm_density(species, "sigungu" if level == "sigungu" else "emd")
    with db.session() as con:
        res["freshness"] = freshness.of(con, "region_stat", "source IN ('mafra_farm_by_area','mafra_farm_by_scale')", [],
                                        date_col=None)
    return res


@app.get("/api/farms/shapes")
def farms_shapes(species: str = "poultry"):
    """읍면동 경계(지오펜스)별 농장 수·사육두수 — 농장 좌표가 없을 때 지도에 면으로 표시."""
    return analysis.farm_shapes(species)


@app.get("/api/visits/near")
def visits_near(event_id: int, km: float = 3, days: int = 21):
    """발생 전 N일 동안 발생 리·반경 안 리에 다녀간 축산차량(유형·시설별 횟수)."""
    try:
        return analysis.visits_near_event(event_id, km, days)
    except KeyError:
        raise HTTPException(404, "event not found")


# ── 철새 이동(위치추적 북상 기록) ──────────────────────────────────────
@app.get("/api/birds")
def birds_list(year: str | None = None, species: str | None = None):
    return birds.query(int(year) if year and year.isdigit() else None, species or None)


@app.post("/api/birds/load")
def birds_load():
    try:
        return birds.load()
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


# ── 뉴스·공지 ─────────────────────────────────────────────────────────
@app.get("/api/news")
def news_list(kind: str | None = None, species: str | None = None, q: str | None = None, relevant: int = 1,
              since: str | None = None, limit: int = 100, offset: int = 0):
    return news.query(kind, species, q, bool(relevant), since, limit, offset)


@app.post("/api/news/refresh")
def news_refresh():
    return {"news": news.run_once("news"), "notice": news.run_once("notice")}


# ── 역대 분석 ─────────────────────────────────────────────────────────
_hist_cache: dict = {}


def _hist(name: str, fn, *args):
    """데이터가 바뀌지 않았으면 계산 결과를 다시 쓴다(발생 건수·차량 방문 일수로 판단)."""
    with db.session() as con:
        stamp = (con.scalar("SELECT COUNT(*) FROM event"), con.scalar("SELECT MAX(collected_at) FROM event"),
                 con.scalar("SELECT COUNT(*) FROM vehicle_visit_day"))
    key = (name, args)
    hit = _hist_cache.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    res = fn(*args)
    if len(_hist_cache) > 200:
        _hist_cache.clear()
    _hist_cache[key] = (stamp, res)
    return res


def _hsp(species: str) -> str:
    if species not in SPECIES:
        raise HTTPException(400, "species: poultry | pig | cattle")
    return species


def _git_version() -> str:
    """지금 돌고 있는 코드의 git 커밋(짧은 해시). 화면에 보여 줘서 서버가 새 코드로 다시 켜졌는지 확인한다."""
    from .settings import ROOT
    try:
        head = (ROOT / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1]
            f = ROOT / ".git" / ref
            if f.exists():
                return f.read_text().strip()[:7]
            packed = ROOT / ".git" / "packed-refs"
            for line in packed.read_text().splitlines() if packed.exists() else []:
                if line.endswith(" " + ref):
                    return line[:7]
        return head[:7]
    except OSError:
        return "?"


_VERSION = {"code": _git_version(), "started_at": None}


@app.get("/api/version")
def version():
    _VERSION["started_at"] = _VERSION["started_at"] or _STARTED
    return {**_VERSION, "disk": _git_version()}


@app.post("/api/wild/load")
def wild_load():
    """data/files 의 야생 검출 목록 파일(이름에 '야생')을 다시 읽는다."""
    return wild.load()


@app.get("/api/weather/backfill")
def weather_backfill():
    return weather_bf.status()


@app.post("/api/weather/backfill")
def weather_backfill_now():
    return weather_bf.start_now()


@app.get("/api/assoc/region")
def assoc_region(species: str = "poultry", sido: str = "", sigungu: str = ""):
    """연관 분석 팝업용: 시군의 농장 목록·읍면동별 농장 수·최근 발생."""
    if species not in SPECIES:
        raise HTTPException(400, "species")
    return assoc.region_detail(species, sido, sigungu)


@app.get("/api/assoc")
def assoc_api(species: str = "poultry", disease: str | None = None):
    """연관 분석 6종(종합 모형·야생 검출 시차·기온·차량 방문·농장 밀집도·철새). 처음엔 계산 중 상태를 돌려준다."""
    if species not in SPECIES:
        return {"status": "na"}
    d = disease if disease not in (None, "", "all") else SPECIES[species]["main_disease"]
    return assoc.get(species, d, wait=2)


@app.get("/api/history/{kind}")
def history_api(kind: str, species: str = "poultry", disease: str | None = None):
    sp = _hsp(species)
    d = None if disease in (None, "", "all") else disease
    fns = {"seasons": history.season_compare, "regions": history.region_matrix, "months": history.month_matrix,
           "repeats": history.repeat_places, "visits": history.visit_case_control}
    if kind == "diseases":
        return _hist(kind, history.disease_years, sp)
    if kind not in fns:
        raise HTTPException(404, "unknown analysis")
    res = _hist(kind, fns[kind], sp, d)
    if kind in ("seasons", "regions", "months", "repeats"):
        where, params = analysis.species_filter(sp)
        with db.session() as con:
            res = {**res, "freshness": freshness.of(con, "event", where, params)}
    return res


# ── 데이터 조회(원천별) ───────────────────────────────────────────────
def _xargs(request: Request) -> dict:
    qp = request.query_params
    return {"q": qp.get("q") or None, "date_from": qp.get("from") or None, "date_to": qp.get("to") or None,
            "filters": {k[2:]: v for k, v in qp.items() if k.startswith("f_") and v != ""}}


def _xsrc(sid: str) -> None:
    try:
        explore._src(sid)
    except KeyError:
        raise HTTPException(404, "unknown source")


@app.get("/api/explore")
def explore_catalog():
    return explore.catalog()


@app.get("/api/explore/{sid}/table")
def explore_table(sid: str, request: Request, sort: str | None = None, desc: int = 1, limit: int = 100, offset: int = 0):
    _xsrc(sid)
    return explore.table(sid, **_xargs(request), sort=sort, desc=bool(desc), limit=limit, offset=offset)


@app.get("/api/explore/{sid}/facet/{col}")
def explore_facet(sid: str, col: str, request: Request):
    _xsrc(sid)
    try:
        return explore.facet(sid, col, **_xargs(request))
    except KeyError:
        raise HTTPException(404, "unknown column")


@app.get("/api/explore/{sid}/timeline")
def explore_timeline(sid: str, request: Request):
    _xsrc(sid)
    return explore.timeline(sid, **_xargs(request))


@app.get("/api/explore/{sid}/profile")
def explore_profile(sid: str):
    _xsrc(sid)
    return explore.profile(sid)


@app.get("/api/explore/{sid}/raw")
def explore_raw(sid: str, q: str | None = None, limit: int = 50, offset: int = 0):
    return explore.raw(sid, q, limit, offset)


@app.get("/api/explore/record/{table}/{rid}")
def explore_record(table: str, rid: int):
    try:
        return explore.record_raw(table, rid)
    except KeyError:
        raise HTTPException(404, "not found")


@app.get("/api/explore/{sid}/csv")
def explore_csv(sid: str, request: Request, sort: str | None = None, desc: int = 1):
    _xsrc(sid)
    a = _xargs(request)
    name = f"{sid}_{date.today():%Y%m%d}.csv"
    return StreamingResponse(explore.csv_rows(sid, a["q"], a["date_from"], a["date_to"], a["filters"], sort, bool(desc)),
                             media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/visits/probe")
def visits_probe(day: str | None = None, sido: str = "36"):
    """KAHIS 축산차량 방문 조회가 이 서버에서 되는지 접속 방식별로 확인."""
    from . import kahis_visit
    return kahis_visit.probe(day, sido)


@app.post("/api/visits/run")
def visits_run(day: str | None = None):
    """어제·오늘(또는 지정한 날)을 바로 받기."""
    from . import kahis_visit
    return kahis_visit.run([day] if day else None)


@app.get("/api/visits/series")
def visits_series(species: str = "all", days: int = 60):
    return analysis.visit_series(species, days)


@app.get("/api/visits/map")
def visits_map(species: str = "all", days: int = 7, limit: int = 4000):
    return analysis.visit_map(species, days, limit)


@app.get("/api/events")
def events(species: str = "all", kind: str | None = None, disease: str | None = None,
           date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to"),
           bbox: str | None = None, limit: int = 2000):
    where, params = analysis.species_filter(species)
    for cond, val in (("kind = ?", kind), ("disease = ?", disease), ("date >= ?", date_from), ("date <= ?", date_to)):
        if val:
            where += f" AND {cond}"
            params.append(val)
    b = _bbox(bbox)
    if b:
        where += " AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
        params += [b[0], b[2], b[1], b[3]]
    with db.session() as con:
        items = con.rows(f"SELECT id, kind, disease, species, subspecies, date, sampled, sido, sigungu, place, lat, lon, "
                         f"heads, note, source, collected_at FROM event WHERE {where} ORDER BY date DESC, id DESC LIMIT ?",
                         params + [min(limit, 10000)])
        fr = freshness.of(con, "event", where, params)
    return {"items": items, "freshness": fr}


@app.get("/api/facilities")
def facilities(species: str = "all", kind: str | None = None, bbox: str | None = None, limit: int = 5000):
    where, params = "lat IS NOT NULL", []
    if species != "all":
        where += " AND (species = ? OR kind != 'farm')"
        params.append(species)
    if kind:
        where += " AND kind = ?"
        params.append(kind)
    if kind == "farm":
        where += " AND (status IS NULL OR (status NOT LIKE '%폐업%' AND status NOT LIKE '%취소%'))"
    b = _bbox(bbox)
    if b:
        where += " AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
        params += [b[0], b[2], b[1], b[3]]
    with db.session() as con:
        items = con.rows(f"SELECT id, kind, species, subspecies, name, status, size, sido, sigungu, address, lat, lon, source "
                         f"FROM facility WHERE {where} LIMIT ?", params + [min(limit, 20000)])
        fr = freshness.of(con, "facility", where, params, date_col=None)
    return {"items": items, "freshness": fr}


@app.get("/api/events/{event_id}/impact")
def event_impact(event_id: int, species: str | None = None):
    try:
        res = analysis.impact(event_id, species)
    except KeyError:
        raise HTTPException(404, "event not found")
    with db.session() as con:
        res["freshness"] = freshness.merge(
            freshness.of(con, "event", "id = ?", [event_id]),
            freshness.of(con, "facility", "lat IS NOT NULL", date_col=None))
    return res


@app.get("/api/trace")
def trace(farm_key: str, day: str, days: int = 21):
    res = analysis.trace_contacts(farm_key, day, days)
    with db.session() as con:
        res["freshness"] = freshness.of(con, "contact", "1=1", date_col="at")
    return res


@app.get("/api/risk")
def risk(species: str = "poultry", as_of: str | None = None):
    if species not in SPECIES:
        raise HTTPException(400, "species: poultry | pig | cattle")
    res = analysis.risk_index(species, as_of)
    d30 = (date.today() - timedelta(days=30)).isoformat()
    where, params = analysis.species_filter(species)
    with db.session() as con:
        res["freshness"] = freshness.merge(
            freshness.of(con, "facility", "kind='farm' AND species=?", [species], date_col=None),
            freshness.of(con, "event", f"{where} AND date >= ?", params + [d30]),
            freshness.of(con, "context_ts", "source='kma_asos_daily' AND date >= ?",
                         [(date.today() - timedelta(days=7)).isoformat()]))
    return res


def _season_range(species: str, season: int | None) -> tuple[str | None, str | None]:
    if season is None or species not in SPECIES:
        return None, None
    return analysis.season_bounds(season, SPECIES[species]["season_start_month"])


@app.get("/api/seasons")
def seasons(species: str = "poultry", disease: str | None = None):
    if species not in SPECIES:
        raise HTTPException(400, "species: poultry | pig | cattle")
    return {"items": analysis.seasons(species, disease), "main_disease": SPECIES[species]["main_disease"],
            "link": SPECIES[species]["link"], "season_start_month": SPECIES[species]["season_start_month"]}


@app.get("/api/season")
def season(species: str = "poultry", year: int | None = None, disease: str | None = None,
           link_days: int | None = None, link_km: float | None = None):
    if species not in SPECIES:
        raise HTTPException(400, "species: poultry | pig | cattle")
    if year is None:
        items = analysis.seasons(species, disease)
        if not items:
            return {"events": [], "summary": {}, "freshness": [], "link": SPECIES[species]["link"]}
        year = items[0]["year"]
    res = analysis.season_events(species, year, disease, link_days, link_km)
    where, params = analysis.species_filter(species)
    with db.session() as con:
        res["freshness"] = freshness.of(con, "event", f"{where} AND date BETWEEN ? AND ?", params + [res["from"], res["to"]])
    return res


@app.get("/api/series")
def series(species: str = "poultry", since: str | None = None, season: int | None = None, disease: str | None = None):
    where, params = analysis.species_filter(species)
    s_from, s_to = _season_range(species, season)
    if s_from:
        with db.session() as con:
            fr = freshness.of(con, "event", f"{where} AND date BETWEEN ? AND ?", params + [s_from, s_to])
        return {"items": analysis.weekly_series(species, s_from, s_to, disease), "freshness": fr,
                "from": s_from, "to": s_to}
    with db.session() as con:
        if not since:
            latest = con.scalar(f"SELECT MAX(date) FROM event WHERE {where}", params) or date.today().isoformat()
            since = (date.fromisoformat(latest) - timedelta(days=730)).isoformat()   # 데이터 최신일 기준 2년
        fr = freshness.of(con, "event", f"{where} AND date >= ?", params + [since])
    return {"items": analysis.weekly_series(species, since), "freshness": fr}


@app.get("/api/spacetime")
def spacetime(species: str = "poultry", max_km: float = 20, max_days: int = 14, since: str | None = None,
              season: int | None = None, disease: str | None = None):
    s_from, s_to = _season_range(species, season)
    if not (s_from or since):        # 기간을 안 주면 최근 2년(전 기간 쌍 비교는 너무 느림)
        since = (date.today() - timedelta(days=730)).isoformat()
    res = analysis.spacetime_pairs(species, max_km, max_days, s_from or since, s_to, disease)
    where, params = analysis.species_filter(species)
    with db.session() as con:
        res["freshness"] = freshness.of(con, "event", f"{where} AND lat IS NOT NULL", params)
    return res


@app.get("/api/weather/latest")
def weather_latest(date_: str | None = Query(None, alias="date")):
    """관측소별 가장 최근(또는 지정한 날) 일평균 기온·습도 + 위치 — 지도 기온 점."""
    from .stations import ASOS
    with db.session() as con:
        day = date_ or con.scalar("SELECT MAX(date) FROM context_ts WHERE source='kma_asos_daily' AND metric='temp_avg'")
        rows = con.rows("SELECT place, metric, value FROM context_ts WHERE source='kma_asos_daily' AND date = ? "
                        "AND metric IN ('temp_avg','temp_min','humidity_avg')", [day]) if day else []
        fr = freshness.of(con, "context_ts", "source='kma_asos_daily'", [])
    by: dict = {}
    for r in rows:
        stn = str(r["place"]).split(":")[0]
        if stn in ASOS:
            n, lat, lon = ASOS[stn]
            by.setdefault(stn, {"stn": stn, "name": n, "lat": lat, "lon": lon})[r["metric"]] = r["value"]
    return {"date": str(day)[:10] if day else None, "items": list(by.values()), "freshness": fr}


@app.get("/api/weather")
def weather(place: str | None = None, days: int = 60):
    where, params = "source='kma_asos_daily' AND date >= ?", [(date.today() - timedelta(days=days)).isoformat()]
    if place:
        where += " AND place LIKE ?"
        params.append(f"%{place}%")
    with db.session() as con:
        items = con.rows(f"SELECT place, date, metric, value FROM context_ts WHERE {where} ORDER BY date", params)
        fr = freshness.of(con, "context_ts", where, params)
    return {"items": items, "freshness": fr}


DATA_COLUMNS = {
    "event": "id, date, kind, disease, species, subspecies, sido, sigungu, place, legal_code, lat, lon, heads, note, collected_at",
    "facility": "id, kind, species, subspecies, name, status, size, sido, sigungu, address, lat, lon, collected_at",
    "contact": "id, farm_key, at, purpose, party, collected_at",
    "context_ts": "place, date, metric, value, lat, lon, collected_at",
    "region_stat": "sido, sigungu, emd, species, metric, value, as_of, collected_at",
    "vehicle_visit": "v.date, p.address, k.facility, k.species_raw, k.vehicle, v.n, v.first_hour, v.last_hour",
}
DATA_ORDER = {"event": "date DESC, id DESC", "facility": "sido, sigungu, id", "contact": "at DESC",
              "context_ts": "date DESC, place, metric", "region_stat": "as_of DESC, sido, sigungu, emd, metric",
              "vehicle_visit": "v.date DESC, v.n DESC"}


@app.get("/api/data/{sid}")
def data_browse(sid: str, q: str | None = None, limit: int = 200, offset: int = 0):
    """원천 하나의 정규화된 데이터를 표로 보기 위한 조회(최신순). q: 지역·질병·지표 텍스트 검색."""
    try:
        src = collect.get_source(sid)
    except KeyError:
        if sid != "manual_ui":
            raise HTTPException(404, "unknown source")
        src = {"id": sid, "target": "event", "title": "화면 직접 입력", "title_en": "Manual entry"}
    t = src["target"]
    where, params = "source = ?", [sid]
    if q:
        like = f"%{q}%"
        cols = {"event": ["disease", "place", "sido", "sigungu"], "facility": ["name", "address", "sido", "sigungu"],
                "contact": ["farm_key", "party", "purpose"], "context_ts": ["place", "metric"],
                "region_stat": ["sido", "sigungu", "emd", "metric", "species"],
                "vehicle_visit": ["p.address", "k.facility", "k.vehicle", "k.species_raw"]}[t]
        where += " AND (" + " OR ".join(f"{c} LIKE ?" for c in cols) + ")"
        params += [like] * len(cols)
    if t == "vehicle_visit":    # 번호표를 풀어서 보여 주고, 원천 조건은 필요 없음
        tbl = analysis.VV
        where, params = (where.replace("source = ?", "1=1"), params[1:])
        with db.session() as con:
            total = con.scalar(f"SELECT COUNT(*) FROM {tbl} WHERE {where}", params)
            items = con.rows(f"SELECT {DATA_COLUMNS[t]} FROM {tbl} WHERE {where} ORDER BY {DATA_ORDER[t]} LIMIT ? OFFSET ?",
                             params + [min(limit, 2000), offset])
            fr = freshness.visits(con)
        return {"source": sid, "title": src.get("title"), "title_en": src.get("title_en"), "table": t,
                "total": total, "items": items, "freshness": fr}
    with db.session() as con:
        total = con.scalar(f"SELECT COUNT(*) FROM {t} WHERE {where}", params)
        items = con.rows(f"SELECT {DATA_COLUMNS[t]} FROM {t} WHERE {where} ORDER BY {DATA_ORDER[t]} LIMIT ? OFFSET ?",
                         params + [min(limit, 2000), offset])
        date_col = {"event": "date", "facility": None, "contact": "at", "context_ts": "date", "region_stat": "as_of",
                    "vehicle_visit": "date"}[t]
        fr = freshness.of(con, t, "source = ?", [sid], date_col=date_col)
    return {"source": sid, "title": src.get("title"), "title_en": src.get("title_en"), "table": t,
            "total": total, "items": items, "freshness": fr}


@app.get("/api/freshness")
def freshness_overview():
    return freshness.overview()


@app.get("/api/sources")
def sources():
    with db.session() as con:
        last = {r["source"]: r for r in con.rows(
            "SELECT source, started_at, finished_at, ok, rows, message FROM collect_log WHERE id IN "
            "(SELECT MAX(id) FROM collect_log GROUP BY source)")}
        counts = {t: con.scalar(f"SELECT COUNT(*) FROM {t}")
                  for t in ("event", "facility", "contact", "context_ts", "region_stat", "vehicle_visit", "raw_record")}
    fresh = {f["source"]: f for f in freshness.overview()}
    return {"tables": counts, "dialect": db.engine.dialect.name,
            "sources": [{"id": s["id"], "title": s["title"], "title_en": s.get("title_en", s["title"]),
                         "provider": s["provider"], "target": s["target"], "refresh": s.get("refresh"),
                         "configured": collect.is_configured(s), "last": last.get(s["id"]),
                         "freshness": fresh.get(s["id"])}
                        for s in collect.load_sources()]}


@app.get("/api/auto")
def auto_status():
    with db.session() as con:
        hist = con.rows("SELECT source, started_at, finished_at, ok, rows, message FROM collect_log "
                        "WHERE source LIKE 'auto:%' ORDER BY id DESC LIMIT 5")
    return {**scheduler.status(), "history": hist}


@app.post("/api/auto/run")
def auto_run():
    import threading

    if scheduler.status().get("running"):
        return {"started": False, "reason": "already running"}
    threading.Thread(target=scheduler.run_batch, args=("daily",), daemon=True).start()
    return {"started": True}


@app.post("/api/collect/{sid}")
def run_collect(sid: str, limit: int | None = None):
    try:
        return {"source": sid, "rows": collect.run(sid, limit)}
    except KeyError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


class EventIn(BaseModel):
    kind: str = Field(..., pattern="^(outbreak|wild_detection|movement_restriction|surveillance_positive|alert)$")
    disease: str
    species: str = Field(..., pattern="^(poultry|pig|cattle|wild_bird|wild_boar)$")
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    subspecies: str | None = None
    sampled: str | None = None
    sido: str | None = None
    sigungu: str | None = None
    place: str | None = None
    lat: float | None = None
    lon: float | None = None
    heads: int | None = None
    note: str | None = None


@app.post("/api/events")
def add_event(ev: EventIn):
    row = ev.model_dump()
    row["disease"] = disease_code(row["disease"]) or row["disease"]
    row["source"] = "manual_ui"
    row["uid"] = collect.make_uid("manual_ui", [row["kind"], row["date"], row["place"], row["disease"]])
    with db.session() as con:
        db.save_raw(con, "manual_ui", f"{db.now():%Y%m%d%H%M%S}", [ev.model_dump()])
        db.upsert(con, "event", [row])
        new_id = con.scalar("SELECT id FROM event WHERE uid=?", [row["uid"]])
    return {"id": new_id}


def _asset_ver() -> str:
    """화면 파일(js·css)이 바뀌면 값이 바뀜 → 브라우저가 예전 파일을 캐시에서 쓰지 않게 주소에 붙인다."""
    return str(int(max((WEB_DIR / f).stat().st_mtime for f in ("app.js", "i18n.js", "style.css"))))


@app.get("/")
def index():
    v = _asset_ver()
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for f in ("app.js", "i18n.js", "style.css"):
        html = html.replace(f'/static/{f}"', f'/static/{f}?v={v}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.middleware("http")
async def _no_stale_assets(request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/config.js":
        resp.headers["Cache-Control"] = "no-cache"      # 바뀌었는지 매번 확인(안 바뀌면 304로 빠름)
    return resp


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
