"""야생 검출 과거 목록 불러오기.

국립야생동물질병관리원 '국내외발생현황' 게시판의 시즌별 첨부파일
(예: ''22~'23년 동절기 야생조류 고병원성 AI 발생현황(‘23.4.10. 기준).hwpx')을
data/files/ 에 넣으면 표를 읽어 event(kind=wild_detection) 로 넣는다. hwpx·xlsx·csv 를 읽는다(.hwp 는 한글에서
'다른 이름으로 저장 → hwpx' 후 사용).

표 머리글에서 날짜(확진·검출·채취)·시도·시군·지점 열을 찾아 쓰고, 좌표는 브이월드로
지점 검색 → 읍면동 대표점 → 시군 대표점 순서로 찾는다(geo_cache 에 보관).
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import time
import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx

from . import db, geocode
from .analysis import district_key
from .settings import ROOT, VWORLD_DOMAIN, VWORLD_KEY

FILES = ROOT / "data" / "files"
PAT = re.compile(r"야생|wild", re.I)


# ── 파일 → 표(행 목록) ─────────────────────────────────────────────────
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def hwpx_tables(path: Path) -> list[list[list[str]]]:
    """hwpx(OWPML) 안의 표를 셀 주소(colAddr/rowAddr)와 병합(rowSpan/colSpan)을 반영해 2차원 목록으로."""
    out = []
    with zipfile.ZipFile(path) as z:
        for name in sorted(n for n in z.namelist() if re.match(r"Contents/section\d+\.xml", n)):
            root = ET.fromstring(z.read(name))
            for tbl in root.iter():
                if _local(tbl.tag) != "tbl":
                    continue
                cells = {}
                for tc in tbl.iter():
                    if _local(tc.tag) != "tc":
                        continue
                    addr = next((e for e in tc if _local(e.tag) == "cellAddr"), None)
                    span = next((e for e in tc if _local(e.tag) == "cellSpan"), None)
                    if addr is None:
                        continue
                    r, c = int(addr.get("rowAddr", 0)), int(addr.get("colAddr", 0))
                    rs = int(span.get("rowSpan", 1)) if span is not None else 1
                    cs = int(span.get("colSpan", 1)) if span is not None else 1
                    text = " ".join("".join(t.text or "" for t in p.iter() if _local(t.tag) == "t").strip()
                                    for p in tc.iter() if _local(p.tag) == "p").strip()
                    for dr in range(rs):
                        for dc in range(cs):
                            cells.setdefault((r + dr, c + dc), text)
                if not cells:
                    continue
                nr = max(k[0] for k in cells) + 1
                nc = max(k[1] for k in cells) + 1
                out.append([[cells.get((i, j), "") for j in range(nc)] for i in range(nr)])
    return out


def file_tables(path: Path) -> list[list[list[str]]]:
    suf = path.suffix.lower()
    if suf == ".hwpx":
        return hwpx_tables(path)
    if suf in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        return [[["" if v is None else str(v).strip() for v in row] for row in ws.iter_rows(values_only=True)] for ws in wb.worksheets]
    if suf == ".csv":
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "cp949"):
            try:
                return [list(csv.reader(io.StringIO(raw.decode(enc))))]
            except UnicodeDecodeError:
                continue
    return []


# ── 표 → 검출 목록 ─────────────────────────────────────────────────────
COLS = {   # 머리글에 이 말이 들어 있으면 그 열(앞에 있을수록 우선)
    "date": ["확진일", "확진", "검출일", "판정일", "발생일", "시료채취일", "채취일", "일자", "날짜"],
    "sampled": ["채취"],
    "sido": ["시도", "시·도"],
    "sigungu": ["시군구", "시군", "시·군"],
    "place": ["지점", "장소", "검출지", "검출지역", "발생지", "위치", "지역", "주소"],
    "species": ["종명", "조류명", "종"],
    "kind": ["시료", "구분", "유형"],
    "sero": ["혈청", "항원", "H5"],
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _header(rows: list[list[str]]) -> tuple[int, dict] | None:
    for i, row in enumerate(rows[:6]):
        cells = [_norm(c) for c in row]
        cols = {}
        for key, words in COLS.items():
            for w in words:
                j = next((j for j, c in enumerate(cells) if w in c and j not in cols.values()), None)
                if j is not None:
                    cols[key] = j
                    break
        if "date" in cols and ({"sigungu", "place", "sido"} & set(cols)):
            if cols.get("sampled") == cols.get("date"):
                cols.pop("sampled")
            return i, cols
    return None


def season_year(name: str) -> int | None:
    m = re.search(r"'?(\d{2})\s*[~∼\-–]\s*'?(\d{2})", name)
    if m and int(m.group(2)) == (int(m.group(1)) + 1) % 100:
        return 2000 + int(m.group(1))
    m = re.search(r"(20\d{2})", name)
    return int(m.group(1)) if m else None


def parse_date(s: str, y0: int | None) -> date | None:
    """'22.11.10 / 2022.11.10. / 11.10 / 11/10 / 2022-11-10(목) → 날짜. 연도가 없으면 시즌(9~12월은 앞 해)으로."""
    s = re.sub(r"\(.*?\)", "", str(s or "")).strip().replace("’", "'")
    if not s:
        return None
    m = re.search(r"(?:'?(\d{2,4})[.\-/년]\s*)?(\d{1,2})[.\-/월]\s*(\d{1,2})", s)
    if not m:
        return None
    yy, mm, dd = m.group(1), int(m.group(2)), int(m.group(3))
    if yy:
        y = int(yy) + (2000 if len(yy) == 2 else 0)
    elif y0:
        y = y0 if mm >= 7 else y0 + 1
    else:
        return None
    try:
        return date(y, mm, dd)
    except ValueError:
        return None


SIDO_RE = re.compile(r"^(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주|충청|전라|경상)")


def _split_region(text: str) -> tuple[str, str, str]:
    toks = [t for t in re.split(r"[\s,/]+", text or "") if t]
    sido = toks.pop(0) if toks and SIDO_RE.match(toks[0]) else ""
    sg = toks.pop(0) if toks and re.search(r"(시|군|구)$", toks[0]) else ""
    return sido, sg, " ".join(toks)


def rows_from_tables(tables: list, y0: int | None) -> list[dict]:
    out = []
    for tb in tables:
        h = _header(tb)
        if not h:
            continue
        i0, cols = h
        for row in tb[i0 + 1:]:
            g = lambda k: (row[cols[k]].strip() if k in cols and cols[k] < len(row) else "")  # noqa: E731
            if any("저병원성" in c for c in row) or any("합계" in c or c.strip() == "계" for c in row[:2]):
                continue
            d = parse_date(g("date"), y0)
            if not d:
                continue
            sido, sg, place = g("sido"), g("sigungu"), g("place")
            if not sg:
                s2, g2, p2 = _split_region(place)
                sido, sg, place = sido or s2, g2, p2
            elif not sido and SIDO_RE.match(sg):
                sido, sg, rest = _split_region(sg)
                place = " ".join(x for x in (rest, place) if x)
            if not sg:
                continue
            out.append({"date": d, "sampled": parse_date(g("sampled"), y0), "sido": sido, "sigungu": sg, "place": place,
                        "species_name": g("species"), "kind": g("kind"), "sero": g("sero")})
    return out


# ── 좌표 ───────────────────────────────────────────────────────────────
def _place_search(client: httpx.Client, query: str, sigungu: str) -> tuple[float | None, float | None]:
    params = {"service": "search", "request": "search", "version": "2.0", "crs": "EPSG:4326", "size": 10, "page": 1,
              "type": "place", "format": "json", "errorformat": "json", "query": query, "key": VWORLD_KEY}
    if VWORLD_DOMAIN:
        params["domain"] = VWORLD_DOMAIN
    r = client.get(geocode.SEARCH, params=params)
    r.raise_for_status()
    items = ((r.json().get("response") or {}).get("result") or {}).get("items") or []
    for it in items:
        addr = (it.get("address") or {}).get("parcel") or (it.get("address") or {}).get("road") or ""
        if sigungu and sigungu[:2] in addr:
            return float(it["point"]["y"]), float(it["point"]["x"])
    return None, None


def locate(client: httpx.Client | None, sido: str, sg: str, place: str) -> tuple[float | None, float | None, str]:
    """지점 검색 → 읍면동 대표점 → 시군 대표점. 결과는 geo_cache('wild:…', 'district:…')에 둔다."""
    key = f"wild:{sido}|{sg}|{place}"
    with db.session() as con:
        hit = con.rows("SELECT lat, lon, provider FROM geo_cache WHERE address = ?", [key])
    if hit:
        return hit[0]["lat"], hit[0]["lon"], hit[0]["provider"]
    lat = lon = None
    how = "none"
    if client and VWORLD_KEY:
        try:
            emd = next((t for t in place.split() if re.search(r"(읍|면|동|리)$", t)), "")
            if place and not emd:
                lat, lon = _place_search(client, f"{sg} {place}", sg)
                how = "place" if lat else how
            if lat is None and emd:
                st, lat, lon = geocode.district(client, sido, sg, emd if not emd.endswith("리") else "")
                how = "emd" if st == "OK" else how
            if lat is None:
                st, lat, lon = geocode.district(client, sido, sg, "")
                how = "sigungu" if st == "OK" else how
            time.sleep(0.05)
        except Exception as e:      # 좌표 실패는 건너뛰고 나중에 다시
            print("wild locate", key, e)
            return None, None, "error"
    if lat is None:                 # 브이월드가 안 되면 이미 있는 시군 대표점
        with db.session() as con:
            r = con.rows("SELECT lat, lon FROM geo_cache WHERE address LIKE ? AND status = 'OK'", [f"district:%|{sg}|"])
        if r:
            lat, lon, how = r[0]["lat"], r[0]["lon"], "sigungu"
    with db.session() as con:
        db.upsert(con, "geo_cache", [{"address": key, "lat": lat, "lon": lon, "status": "OK" if lat else "NOT_FOUND",
                                      "provider": how, "at": db.now()}])
    return lat, lon, how


HOW = {"place": "지점 검색", "emd": "읍면동 대표점", "sigungu": "시군 대표점"}


def load(files: list[Path] | None = None) -> dict:
    files = files or sorted(p for p in FILES.glob("*") if PAT.search(p.name) and p.suffix.lower() in (".hwpx", ".xlsx", ".xlsm", ".csv"))
    report, events = [], []
    with httpx.Client(timeout=20) as client:
        for f in files:
            try:
                tables = file_tables(f)
                rows = rows_from_tables(tables, season_year(f.name))
            except Exception as e:
                report.append({"file": f.name, "error": str(e)[:200], "rows": 0})
                continue
            n = 0
            for r in rows:
                lat, lon, how = locate(client, r["sido"], r["sigungu"], r["place"])
                key = f"{r['date']}|{r['sido']}|{r['sigungu']}|{r['place']}|{r['species_name']}|{r['kind']}"
                events.append({"uid": "wild:" + hashlib.sha1(key.encode()).hexdigest()[:20], "kind": "wild_detection",
                               "disease": "HPAI", "species": "wild_bird",
                               "subspecies": " ".join(x for x in (r["species_name"], r["kind"]) if x)[:64] or None,
                               "date": r["date"], "sampled": r["sampled"], "sido": r["sido"], "sigungu": r["sigungu"],
                               "place": r["place"] or r["sigungu"], "lat": lat, "lon": lon,
                               "note": f"{r['sero']} · 좌표: {HOW.get(how, '없음')} · 출처: {f.name}".strip(" ·"),
                               "source": "wild_files", "collected_at": db.now()})
                n += 1
            report.append({"file": f.name, "tables": len(tables), "rows": n,
                           "located": sum(1 for e in events[-n:] if e["lat"] is not None) if n else 0})
    if events:
        with db.session() as con:
            db.upsert(con, "event", events)
    return {"files": report, "rows": len(events)}
