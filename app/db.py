"""저장소 — PostgreSQL(운영) / SQLite(테스트·간이 실행) 겸용.

DATABASE_URL 로 고른다.
  postgresql+psycopg://ldt:ldt@localhost:5433/ldt   ← docker-compose.yml 의 기본값
  sqlite:///data/ldt.sqlite3                         ← 미설정 시

테이블
  raw_record  : API·파일에서 받은 원본 레코드 그대로(JSONB). 보고서·재분석의 원천.
  event       : 발생·검출·이동제한·양성·경보 1건
  facility    : 농장, 도축장, 거점소독시설, 인공수정소, 철새도래지
  contact     : 시설 간 접촉 1회(차량 농장 진입, 인공수정 방문)
  context_ts  : 지역·지점 시계열(기상, 철새, 도축량, 계란 판정량)
  region_stat : 시군 단위 기준값(농장 수, 두수, 규모 구간별 호수)
  collect_log : 수집 실행 이력(최신성 표시의 근거)

모든 정규화 테이블에 collected_at(적재 시각, UTC)이 있어 "데이터가 언제 기준인지"를 항상 답할 수 있다.
좌표는 WGS84 lat/lon. PostGIS 없이도 동작하도록 bbox + haversine 으로 공간 질의를 한다.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import (JSON, BigInteger, Boolean, Column, Date, DateTime, Float, Index, Integer, MetaData,
                        String, Table, Text, UniqueConstraint, create_engine, text)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Connection

from .settings import DATABASE_URL

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
IS_PG = engine.dialect.name == "postgresql"

JSONType = JSON().with_variant(postgresql.JSONB(), "postgresql")
PK = BigInteger().with_variant(Integer(), "sqlite")   # SQLite 는 INTEGER PRIMARY KEY 여야 자동 증가
TS = DateTime(timezone=True)

md = MetaData()


def _pk() -> Column:
    return Column("id", PK, primary_key=True, autoincrement=True)


def _collected() -> Column:
    return Column("collected_at", TS, nullable=False)


raw_record = Table(
    "raw_record", md, _pk(),
    Column("source", String(64), nullable=False),
    Column("fetched_at", TS, nullable=False),
    Column("batch", String(32), nullable=False),        # 같은 수집 실행의 묶음
    Column("payload", JSONType, nullable=False),
    Index("ix_raw_source_time", "source", "fetched_at"),
)

event = Table(
    "event", md, _pk(),
    Column("uid", String(80), nullable=False, unique=True),
    Column("kind", String(32), nullable=False),
    Column("disease", String(80)), Column("species", String(16)), Column("subspecies", String(64)),
    Column("date", Date, nullable=False), Column("sampled", Date),
    Column("sido", String(40)), Column("sigungu", String(40)), Column("place", Text),
    Column("legal_code", String(10)),                    # 법정동 코드(10자리) — 좌표 변환·집계 키
    Column("lat", Float), Column("lon", Float), Column("heads", Integer), Column("note", Text),
    Column("source", String(64), nullable=False), Column("raw", JSONType), _collected(),
    Index("ix_event_date", "date"), Index("ix_event_sp", "species", "date"),
)

facility = Table(
    "facility", md, _pk(),
    Column("uid", String(80), nullable=False, unique=True),
    Column("kind", String(32), nullable=False),
    Column("species", String(16)), Column("subspecies", String(64)), Column("name", Text),
    Column("status", String(40)), Column("size", BigInteger),
    Column("sido", String(40)), Column("sigungu", String(40)), Column("address", Text),
    Column("lat", Float), Column("lon", Float),
    Column("source", String(64), nullable=False), Column("raw", JSONType), _collected(),
    Index("ix_fac_geo", "lat", "lon"), Index("ix_fac_kind", "kind", "species"),
)

contact = Table(
    "contact", md, _pk(),
    Column("uid", String(80), nullable=False, unique=True),
    Column("farm_key", String(80)), Column("at", DateTime, nullable=False),
    Column("purpose", String(80)), Column("party", Text),
    Column("source", String(64), nullable=False), Column("raw", JSONType), _collected(),
    Index("ix_contact_party", "party", "at"), Index("ix_contact_farm", "farm_key", "at"),
)

context_ts = Table(
    "context_ts", md, _pk(),
    Column("place", String(120), nullable=False), Column("date", Date, nullable=False),
    Column("metric", String(120), nullable=False), Column("value", Float),
    Column("lat", Float), Column("lon", Float),
    Column("source", String(64), nullable=False), _collected(),
    UniqueConstraint("place", "date", "metric", "source", name="uq_context_ts"),
)

region_stat = Table(
    "region_stat", md, _pk(),
    Column("sido", String(40), nullable=False, default=""), Column("sigungu", String(40), nullable=False, default=""),
    Column("emd", String(40), nullable=False, default=""),
    Column("species", String(16), nullable=False, default=""), Column("metric", String(200), nullable=False),
    Column("value", Float), Column("as_of", String(10), nullable=False, default=""),
    Column("source", String(64), nullable=False), _collected(),
    UniqueConstraint("sido", "sigungu", "emd", "species", "metric", "as_of", "source", name="uq_region_stat2"),
)

geo_cache = Table(
    "geo_cache", md,
    Column("address", String(200), primary_key=True),
    Column("lat", Float), Column("lon", Float),
    Column("status", String(20), nullable=False),        # OK | NOT_FOUND | ERROR
    Column("provider", String(20), nullable=False),
    Column("at", TS, nullable=False),
)

# 읍면동 경계(브이월드 LT_C_ADEMD_INFO, 단순화한 GeoJSON geometry 문자열)
region_shape = Table(
    "region_shape", md,
    Column("code", String(10), primary_key=True),
    Column("sido_cd", String(2), nullable=False), Column("sigungu", String(40), nullable=False),
    Column("emd", String(40), nullable=False), Column("full_nm", String(120)),
    Column("lat", Float), Column("lon", Float),
    Column("geom", Text, nullable=False), Column("at", TS, nullable=False),
)

# KAHIS 축산차량 시설방문 — 하루 전국 약 17만 건이라 (날짜, 리, 방문 종류)별 횟수로 묶고,
# 리 주소와 방문 종류(시설구분·축종·차량유형)는 번호표로 따로 둬서 행을 작게 유지한다.
visit_place = Table(
    "visit_place", md, Column("id", Integer, primary_key=True, autoincrement=True),
    Column("address", String(150), nullable=False, unique=True),
    Column("sido", String(40)), Column("sigungu", String(40)), Column("emd", String(40)), Column("li", String(40)),
)

visit_kind = Table(
    "visit_kind", md, Column("id", Integer, primary_key=True, autoincrement=True),
    Column("facility", String(40), nullable=False), Column("species_raw", String(40), nullable=False),
    Column("species", String(16)), Column("vehicle", String(40), nullable=False),
    UniqueConstraint("facility", "species_raw", "vehicle", name="uq_visit_kind"),
)

vehicle_visit = Table(
    "vehicle_visit", md,
    Column("date", Date, primary_key=True), Column("place_id", Integer, primary_key=True),
    Column("kind_id", Integer, primary_key=True), Column("n", Integer, nullable=False),
    Column("first_hour", Integer), Column("last_hour", Integer),
    Index("ix_vv_place", "place_id", "date"),
)

vehicle_visit_day = Table(
    "vehicle_visit_day", md,
    Column("date", Date, primary_key=True), Column("visits", Integer), Column("groups", Integer),
    Column("at", TS, nullable=False),
)

# 철새 위치추적 북상 기록(검역본부) — 출발·도착 좌표와 방위
bird_track = Table(
    "bird_track", md, _pk(),
    Column("uid", String(160), nullable=False, unique=True),
    Column("year", Integer), Column("depart", Date), Column("species", String(40), nullable=False), Column("tag", String(40)),
    Column("origin", String(80)), Column("origin_lat", Float), Column("origin_lon", Float), Column("origin_match", String(80)),
    Column("dest", String(80)), Column("dest_country", String(20)), Column("dest_lat", Float), Column("dest_lon", Float),
    Column("dest_precision", String(10)), Column("dest_match", String(80)),
    Column("distance_km", Float), Column("bearing", Float), Column("compass", String(8)), Column("compass_en", String(4)),
    Column("clock", Integer), Column("source", String(64), nullable=False), _collected(),
)

# 뉴스·공지 모니터링
news_item = Table(
    "news_item", md, _pk(),
    Column("uid", String(32), nullable=False, unique=True),
    Column("kind", String(16), nullable=False), Column("source", String(40), nullable=False),
    Column("publisher", String(80)), Column("title", Text, nullable=False), Column("link", Text),
    Column("summary", Text), Column("published", TS), Column("species", String(40)), Column("query", String(120)),
    Column("relevant", Boolean, nullable=False, default=True), Column("fetched_at", TS, nullable=False),
    Index("ix_news_time", "published"), Index("ix_news_kind", "kind", "relevant"),
)

collect_log = Table(
    "collect_log", md, _pk(),
    Column("source", String(64), nullable=False),
    Column("started_at", TS, nullable=False), Column("finished_at", TS),
    Column("ok", Boolean, nullable=False), Column("rows", Integer), Column("message", Text),
)

TABLES = {t.name: t for t in (raw_record, event, facility, contact, context_ts, region_stat, geo_cache, region_shape, visit_place, visit_kind, vehicle_visit, vehicle_visit_day, news_item, bird_track, collect_log)}
CONFLICT = {"event": ["uid"], "facility": ["uid"], "contact": ["uid"],
            "context_ts": ["place", "date", "metric", "source"],
            "region_stat": ["sido", "sigungu", "emd", "species", "metric", "as_of", "source"],
            "geo_cache": ["address"], "region_shape": ["code"],
            "vehicle_visit": ["date", "place_id", "kind_id"], "vehicle_visit_day": ["date"],
            "visit_place": ["address"], "news_item": ["uid"], "bird_track": ["uid"], "visit_kind": ["facility", "species_raw", "vehicle"]}


def now() -> datetime:
    return datetime.now(timezone.utc)


def init() -> None:
    md.create_all(engine)
    _migrate()


def _migrate() -> None:
    """이전 버전 DB를 현재 스키마로 맞춘다(추가 컬럼, 키가 바뀐 파생 테이블 재생성)."""
    from sqlalchemy import inspect

    insp = inspect(engine)
    ev_cols = {c["name"] for c in insp.get_columns("event")}
    rs_cols = {c["name"] for c in insp.get_columns("region_stat")}
    with engine.begin() as con:
        if "legal_code" not in ev_cols:
            con.execute(text("ALTER TABLE event ADD COLUMN legal_code VARCHAR(10)"))
        if IS_PG:
            con.execute(text("ALTER TABLE event ALTER COLUMN disease TYPE VARCHAR(80)"))
        if "emd" not in rs_cols:
            # region_stat 은 원천에서 다시 만들 수 있는 집계 테이블이라 새 키로 재생성한다
            region_stat.drop(con)
            region_stat.create(con)
        # v0.7: 축종명(닭/오리 등)을 지표 이름에 넣도록 바뀜 → 이전 형식으로 쌓인 농림부 집계는 지우고 다시 받는다
        stale = con.execute(text(
            "SELECT COUNT(*) FROM region_stat WHERE (source='mafra_farm_by_area' AND metric='farms') "
            "OR (source='mafra_disease_stats' AND metric LIKE 'outbreak_farms:%' AND metric NOT LIKE 'outbreak_farms:%:%')")).scalar()
        # v0.22: 철새 표에 수집 시각
        if "bird_track" in insp.get_table_names() and "collected_at" not in {c["name"] for c in insp.get_columns("bird_track")}:
            con.execute(text(f"ALTER TABLE bird_track ADD COLUMN collected_at {'TIMESTAMPTZ' if IS_PG else 'TIMESTAMP'}"))
        # v0.19: 원천별 조회(데이터 조회 화면)를 빠르게
        for ix, tbl, col in (("ix_event_source", "event", "source"), ("ix_fac_source", "facility", "source"),
                             ("ix_rs_source", "region_stat", "source, as_of"), ("ix_ctx_source", "context_ts", "source, date"),
                             ("ix_contact_source", "contact", "source")):
            con.execute(text(f"CREATE INDEX IF NOT EXISTS {ix} ON {tbl} ({col})"))
        # v0.15: 축산차량 방문 테이블을 번호표 구조로 바꿈 → 이전 형식이면 지우고 다시 받는다
        if "vehicle_visit" in insp.get_table_names() and "address" in {c["name"] for c in insp.get_columns("vehicle_visit")}:
            vehicle_visit.drop(con, checkfirst=True)
            vehicle_visit.create(con)
            con.execute(text("DELETE FROM vehicle_visit_day"))
        # v0.10: 사육규모 원천의 BRD_HEAD_QY 를 구간으로 잘못 읽던 행 → 지우고 사육두수로 다시 받는다
        bad_scale = con.execute(text("SELECT COUNT(*) FROM region_stat WHERE source='mafra_farm_by_scale' "
                                     "AND metric LIKE 'farms_by_scale%'")).scalar()
        if bad_scale:
            con.execute(text("DELETE FROM region_stat WHERE source='mafra_farm_by_scale'"))
            con.execute(text("DELETE FROM collect_log WHERE source='mafra_farm_by_scale'"))
        if stale:
            con.execute(text("DELETE FROM region_stat WHERE source LIKE 'mafra_%'"))
            con.execute(text("DELETE FROM collect_log WHERE source LIKE 'mafra_%' AND source <> 'mafra_disease_outbreak' "
                             "AND source <> 'mafra_disinfection'"))


# ── 실행 래퍼 ──────────────────────────────────────────────────────────
class DB:
    """`?` 자리표시자를 쓰는 짧은 SQL을 두 DB에서 그대로 돌리기 위한 얇은 래퍼."""

    def __init__(self, con: Connection):
        self.con = con

    @staticmethod
    def _bind(sql: str, params: Iterable[Any]) -> tuple[Any, dict]:
        params = list(params)
        i = iter(range(len(params)))
        sql2 = re.sub(r"\?", lambda _: f":p{next(i)}", sql)
        return text(sql2), {f"p{k}": v for k, v in enumerate(params)}

    def rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        q, p = self._bind(sql, params)
        return [_plain(dict(r._mapping)) for r in self.con.execute(q, p)]

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        q, p = self._bind(sql, params)
        return _val(self.con.execute(q, p).scalar())

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        q, p = self._bind(sql, params)
        self.con.execute(q, p)


@contextmanager
def session():
    with engine.begin() as con:
        yield DB(con)


def _val(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def _plain(d: dict) -> dict:
    return {k: _val(v) for k, v in d.items()}


def rows(db: DB, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return db.rows(sql, params)


# ── 적재 ───────────────────────────────────────────────────────────────
def _coerce(table: Table, row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if k not in table.c:
            continue
        col = table.c[k]
        if isinstance(col.type, JSON) and v is not None:
            v = _jsonable(v)
        elif isinstance(v, str) and isinstance(col.type, (Date, DateTime)):
            v = v.strip()
            if not v:
                v = None
            elif isinstance(col.type, DateTime):
                v = datetime.fromisoformat(v if len(v) > 10 else v + " 00:00")
            else:
                v = date.fromisoformat(v[:10])
        out[k] = v
    return out


def upsert(db: DB, table: str, items: Iterable[dict], key: str | None = None, chunk: int = 500) -> int:
    """정해진 고유키로 넣거나 갱신. collected_at 은 지금 시각으로 채운다."""
    t = TABLES[table]
    ins = postgresql.insert if IS_PG else sqlite.insert
    ts = now()
    batch, n = [], 0

    def flush():
        nonlocal batch
        if not batch:
            return
        # 같은 묶음 안의 중복 키는 마지막 값만 남긴다(Postgres ON CONFLICT 제약)
        uniq = {tuple(r.get(c) for c in CONFLICT[table]): r for r in batch}
        stmt = ins(t).values(list(uniq.values()))
        upd = {c.name: stmt.excluded[c.name] for c in t.c if c.name not in ("id", *CONFLICT[table])}
        db.con.execute(stmt.on_conflict_do_update(index_elements=CONFLICT[table], set_=upd))
        batch = []

    cols = set(t.c.keys())
    for r in items:
        r = _coerce(t, r)
        if "collected_at" in cols:
            r["collected_at"] = ts
        for c in cols - set(r) - {"id"}:
            r[c] = t.c[c].default.arg if t.c[c].default is not None else None
        batch.append(r)
        n += 1
        if len(batch) >= chunk:
            flush()
    flush()
    return n


def save_raw(db: DB, source: str, batch: str, records: list[dict]) -> None:
    if not records:
        return
    ts = now()
    db.con.execute(raw_record.insert(), [{"source": source, "fetched_at": ts, "batch": batch,
                                          "payload": _jsonable(r)} for r in records])


def _jsonable(o: Any) -> Any:
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_jsonable(v) for v in o]
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    return o
