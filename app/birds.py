"""검역본부 '철새 이동 정보'(위치추적기 부착 철새의 북상 기록) → 이동 벡터.

원본은 '북상 전 국내 위치'(예: 경기 이천)와 '북상 후 국외 위치'(예: 중국 지린성 옌볜)가 지명뿐이라,
지명 사전(GAZ)으로 좌표를 붙이고 출발→도착 사이 거리·방위각·16방위(북북동 등)·시계 방향(1~12시)을 계산해 저장한다.
지명 좌표는 시·군(국내), 도시·성·주(국외) 대표점이다. 사전에 없으면 앞 단어부터 줄여 찾고(예: '중국 지린성 판시'→'중국 지린성'),
그래도 없으면 나라 대표점을 쓰며 precision 으로 정확도를 남긴다(city > province > country).
"""
from __future__ import annotations

import csv
import io
import math
import re
from datetime import date
from pathlib import Path

from . import db
from .settings import ROOT

FILE_DIR = ROOT / "data" / "files"
SOURCE = "migratory_birds"

# (위도, 경도, 정확도)
GAZ: dict[str, tuple[float, float, str]] = {
    # ── 국내(북상 전) ──
    "부산 을숙도": (35.10, 128.94, "city"), "부산 사하": (35.10, 128.97, "city"), "부산 서구": (35.10, 129.02, "city"),
    "부산 강서": (35.21, 128.98, "city"), "부산광역시": (35.18, 129.08, "city"), "부산": (35.18, 129.08, "city"),
    "충북 진천": (36.86, 127.44, "city"), "충북 청주": (36.64, 127.49, "city"),
    "충남 예산": (36.68, 126.85, "city"), "충남 아산": (36.79, 127.00, "city"), "충남 서산": (36.78, 126.45, "city"),
    "충남 논산": (36.19, 127.10, "city"), "충남 천안": (36.81, 127.15, "city"), "충남 공주": (36.45, 127.12, "city"),
    "충남 당진": (36.89, 126.63, "city"),
    "경기 화성": (37.20, 126.83, "city"), "경기 안성": (37.01, 127.28, "city"), "경기 이천": (37.27, 127.44, "city"),
    "경기 파주": (37.76, 126.78, "city"), "경기 김포": (37.62, 126.72, "city"), "경기 용인": (37.24, 127.18, "city"),
    "경기 평택": (36.99, 127.11, "city"), "경기 연천": (38.10, 127.07, "city"), "경기 동두천": (37.90, 127.06, "city"),
    "경기 가평": (37.83, 127.51, "city"), "경기 광명": (37.48, 126.86, "city"), "경기 여주": (37.30, 127.64, "city"),
    "경기 안산": (37.32, 126.83, "city"), "경기 광주": (37.43, 127.26, "city"),
    "인천 강화": (37.75, 126.49, "city"),
    "강원 삼척": (37.45, 129.17, "city"), "강원 철원": (38.15, 127.31, "city"), "강원 강릉": (37.75, 128.88, "city"),
    "강원 고성": (38.38, 128.47, "city"),
    "전북 익산": (35.95, 126.96, "city"), "전북 정읍": (35.57, 126.86, "city"), "전북 김제": (35.80, 126.89, "city"),
    "전북 전주": (35.82, 127.15, "city"), "전북 남원": (35.42, 127.39, "city"), "전북 임실": (35.62, 127.29, "city"),
    "전남 나주": (35.02, 126.71, "city"), "전남 장성": (35.30, 126.78, "city"),
    "광주광역시": (35.16, 126.85, "city"), "광주광역시 광산구": (35.14, 126.79, "city"),
    "경남 창원": (35.23, 128.68, "city"), "경남 고성": (34.97, 128.32, "city"), "경남 경산": (35.83, 128.74, "city"),
    "경북 영덕": (36.42, 129.37, "city"), "경북 영천": (35.97, 128.94, "city"), "경북 포항": (36.02, 129.34, "city"),
    "경북 경산": (35.83, 128.74, "city"),
    # ── 북한 ──
    "북한": (40.10, 127.30, "country"), "북한 자강도": (40.80, 126.60, "province"), "북한 황해": (38.40, 125.90, "province"),
    "북한 황해남도": (38.20, 125.40, "province"), "북한 황해북도": (38.50, 126.30, "province"),
    "북한 라선": (42.26, 130.30, "city"), "북한 강원도": (38.80, 127.50, "province"), "북한 개성": (37.97, 126.55, "city"),
    "북한 함경북도": (41.80, 129.60, "province"), "북한 함경남도": (40.20, 127.60, "province"),
    "북한 평안도": (39.60, 125.60, "province"), "북한 평안남도": (39.30, 125.80, "province"),
    "북한 평안북도": (40.20, 124.90, "province"), "북한 두만강": (42.40, 130.20, "province"), "북한 청진": (41.80, 129.78, "city"),
    "북한 봉천": (38.06, 125.60, "city"),
    # ── 중국 ──
    "중국": (43.00, 124.00, "country"), "중국 지린성": (43.70, 126.20, "province"), "중국 헤이룽장성": (47.00, 128.00, "province"),
    "중국 랴오닝성": (41.30, 122.60, "province"), "중국 내몽골자치구": (45.50, 118.00, "province"), "중국 허베이성": (39.30, 116.40, "province"),
    "중국 지린성 창춘": (43.88, 125.32, "city"), "중국 지린성 퉁화": (41.73, 125.94, "city"), "중국 지린성 바이청": (45.62, 122.84, "city"),
    "중국 지린성 지린": (43.84, 126.55, "city"), "중국 지린성 옌볜": (42.90, 129.50, "city"), "중국 지린성 쑹위안": (45.14, 124.83, "city"),
    "중국 지린성 판시": (42.95, 126.06, "city"), "중국 지린성 바이산": (41.94, 126.42, "city"), "중국 지린성 쓰핑": (43.17, 124.35, "city"),
    "중국 헤이룽장성 지시": (45.30, 130.97, "city"), "중국 헤이룽장성 하얼빈": (45.80, 126.53, "city"),
    "중국 헤이룽장성 자무쓰": (46.80, 130.32, "city"), "중국 헤이룽장성 다칭": (46.59, 125.10, "city"),
    "중국 헤이룽장성 무단장": (44.55, 129.63, "city"), "중국 헤이룽장성 허강": (47.35, 130.30, "city"),
    "중국 헤이룽장성 치치하얼": (47.35, 123.92, "city"), "중국 헤이룽장성 미산": (45.55, 131.87, "city"),
    "중국 헤이룽장성 쑤이화": (46.65, 126.97, "city"),
    "중국 랴오닝성 푸순": (41.88, 123.96, "city"), "중국 랴오닝성 선양": (41.80, 123.43, "city"),
    "중국 랴오닝성 단둥": (40.12, 124.38, "city"), "중국 랴오닝성 톄닝": (42.29, 123.84, "city"),
    "중국 내몽골자치구 퉁랴오": (43.65, 122.24, "city"), "중국 내몽골자치구 후룬베이얼": (49.21, 119.77, "city"),
    "중국 내몽골자치구 츠펑": (42.26, 118.89, "city"), "중국 허베이성 청더": (40.95, 117.96, "city"),
    # ── 러시아 ──
    "러시아": (48.00, 132.00, "country"), "러시아 프리모르스키": (44.50, 134.00, "province"), "러시아 항카호": (45.00, 132.40, "city"),
    "러시아 슬라반카": (42.86, 131.36, "city"), "러시아 사하공화국": (62.03, 129.73, "province"),
    "러시아 사할린": (48.50, 142.80, "province"), "러시아 부랴티야": (52.00, 108.50, "province"),
    "러시아 아무르": (51.50, 128.00, "province"), "러시아 하바롭스크": (48.48, 135.07, "city"),
    "러시아 하바롭스크 불라바": (51.10, 140.00, "city"),
    # ── 일본 ──
    "일본": (37.00, 138.00, "country"), "일본 홋카이도": (43.40, 142.80, "province"), "일본 토야마현": (36.70, 137.21, "province"),
    "일본 니가타현": (37.90, 139.02, "province"), "일본 돗토리현": (35.50, 134.24, "province"),
    "일본 시마네현": (35.47, 133.05, "province"), "일본 후쿠오카": (33.59, 130.40, "city"),
}
COUNTRY = {"북한": "북한", "중국": "중국", "러시아": "러시아", "일본": "일본"}
COMPASS = ["북", "북북동", "북동", "동북동", "동", "동남동", "남동", "남남동", "남", "남남서", "남서", "서남서", "서", "서북서", "북서", "북북서"]
COMPASS_EN = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
_SIDO = {"서울특별시": "서울", "부산광역시": "부산", "인천광역시": "인천", "경기도": "경기", "강원도": "강원", "충청북도": "충북",
         "충청남도": "충남", "전라북도": "전북", "전라남도": "전남", "경상북도": "경북", "경상남도": "경남"}


def norm(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").replace(" ", " ")).strip()


def locate(name: str) -> tuple[float | None, float | None, str, str]:
    """지명 → (위도, 경도, 정확도, 실제로 찾은 지명). 사전에 없으면 끝 단어부터 줄여 찾는다."""
    n = norm(name)
    if n in GAZ:
        lat, lon, prec = GAZ[n]
        return lat, lon, prec, n
    for k, v in _SIDO.items():
        n = n.replace(k, v)
    parts = n.split()
    for i in range(len(parts), 0, -1):
        key = " ".join(parts[:i])
        if key in GAZ:
            lat, lon, prec = GAZ[key]
            if i < len(parts) and prec == "city":
                prec = "province"
            return lat, lon, prec, key
    return None, None, "none", ""


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """출발점에서 본 도착점의 처음 방위각(진북 0°, 시계 방향)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def compass16(b: float) -> tuple[str, str]:
    i = int((b + 11.25) // 22.5) % 16
    return COMPASS[i], COMPASS_EN[i]


def clock(b: float) -> int:
    """방위각 → 시계 방향(12시 = 북, 3시 = 동)."""
    h = round(b / 30) % 12
    return 12 if h == 0 else h


def parse_date(s: str, year_hint: int | None) -> date | None:
    """"'21.2.10", "‘23.3.13", "2023-03-13" 등."""
    s = (s or "").strip().lstrip("'‘’`")
    m = re.match(r"(\d{2,4})[.\-/](\d{1,2})[.\-/](\d{1,2})", s)
    if not m:
        return None
    y = int(m.group(1))
    y = y + 2000 if y < 100 else y
    try:
        return date(y, int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _rows_from_file(path: Path) -> list[dict]:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        ws = load_workbook(path, read_only=True, data_only=True).active
        it = ws.iter_rows(values_only=True)
        head = [str(h or "").strip() for h in next(it)]
        return [{head[i]: ("" if v is None else str(v)) for i, v in enumerate(r) if i < len(head) and head[i]} for r in it]
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    return [{(k or "").strip(): (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(io.StringIO(text))]


def _pick(r: dict, *names: str) -> str:
    for n in names:
        for k, v in r.items():
            if k.replace(" ", "") == n.replace(" ", "") and v:
                return v
    return ""


def transform(rows: list[dict]) -> tuple[list[dict], list[str]]:
    out, missing = [], set()
    for r in rows:
        y = re.sub(r"\D", "", _pick(r, "연도"))
        year = int(y) if y else None
        d = parse_date(_pick(r, "북상일", "북상일자"), year)
        species = _pick(r, "조류종(부착코드)", "조류종", "종")
        tag = _pick(r, "추적기코드", "부착코드", "위치추적기코드")
        o_name, t_name = norm(_pick(r, "북상전 국내 위치", "북상전국내위치")), norm(_pick(r, "북상후 국외 위치", "북상후국외위치"))
        if not (species and o_name and t_name):
            continue
        olat, olon, oprec, okey = locate(o_name)
        tlat, tlon, tprec, tkey = locate(t_name)
        if olat is None:
            missing.add(o_name)
        if tlat is None:
            missing.add(t_name)
        row = {"uid": f"{year or ''}|{tag}|{d or ''}|{o_name}|{t_name}"[:150], "year": year or (d.year if d else None),
               "depart": d, "species": species[:40], "tag": tag[:40],
               "origin": o_name[:80], "origin_lat": olat, "origin_lon": olon, "origin_match": okey[:80],
               "dest": t_name[:80], "dest_country": COUNTRY.get(t_name.split()[0], t_name.split()[0])[:20],
               "dest_lat": tlat, "dest_lon": tlon, "dest_precision": tprec, "dest_match": tkey[:80],
               "distance_km": None, "bearing": None, "compass": None, "compass_en": None, "clock": None, "source": SOURCE}
        if olat is not None and tlat is not None:
            b = bearing(olat, olon, tlat, tlon)
            row.update(distance_km=round(distance_km(olat, olon, tlat, tlon), 1), bearing=round(b, 1),
                       compass=compass16(b)[0], compass_en=compass16(b)[1], clock=clock(b))
        out.append(row)
    return out, sorted(missing)


def load(path: Path | None = None) -> dict:
    """파일을 읽어 bird_track 을 통째로 바꾼다(같은 파일을 다시 넣어도 중복 없음)."""
    if path is None:
        cands = sorted([p for p in FILE_DIR.glob("*") if re.search(r"철새|migrat", p.name) and p.suffix.lower() in (".csv", ".xlsx")],
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not cands:
            raise FileNotFoundError(f"{FILE_DIR} 에 철새 이동 정보 파일(csv/xlsx)이 없습니다")
        path = cands[0]
    rows, missing = transform(_rows_from_file(path))
    with db.session() as con:
        con.execute("DELETE FROM bird_track WHERE source = ?", [SOURCE])
        db.upsert(con, "bird_track", rows)
        con.con.execute(db.collect_log.insert().values(source=SOURCE, started_at=db.now(), finished_at=db.now(), ok=True,
                                                       rows=len(rows), message=f"{path.name} · {len(rows)}건"
                                                       + (f" · 좌표 못 찾은 지명: {', '.join(missing)}" if missing else "")))
    return {"file": path.name, "rows": len(rows), "missing": missing}


def query(year: int | None = None, species: str | None = None) -> dict:
    w, p = ["1=1"], []
    if year:
        w.append("year = ?"); p.append(year)
    if species:
        w.append("species = ?"); p.append(species)
    with db.session() as con:
        items = con.rows(f"SELECT * FROM bird_track WHERE {' AND '.join(w)} ORDER BY depart, id", p)
        years = [r["year"] for r in con.rows("SELECT DISTINCT year FROM bird_track WHERE year IS NOT NULL ORDER BY year")]
        sp = con.rows("SELECT species, COUNT(*) AS n FROM bird_track GROUP BY species ORDER BY COUNT(*) DESC")
    rose = [0] * 16
    for r in items:
        if r["bearing"] is not None:
            rose[int((r["bearing"] + 11.25) // 22.5) % 16] += 1
    by_country: dict = {}
    for r in items:
        by_country[r["dest_country"]] = by_country.get(r["dest_country"], 0) + 1
    dists = sorted(r["distance_km"] for r in items if r["distance_km"] is not None)
    return {"items": items, "years": years, "species": sp, "rose": rose, "compass": COMPASS, "compass_en": COMPASS_EN,
            "by_country": by_country, "median_km": dists[len(dists) // 2] if dists else None}
