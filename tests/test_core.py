from datetime import date, timedelta

from pyproj import Transformer

from app import analysis, collect, db, geo
from app.species import disease_code, species_code


def setup_module():
    db.init()


def test_epsg5174_roundtrip():
    to5174 = Transformer.from_crs("EPSG:4326", "EPSG:5174", always_xy=True)
    x, y = to5174.transform(127.509, 34.886)  # 순천만
    lat, lon = geo.to_wgs84(x, y, "5174")
    assert abs(lat - 34.886) < 1e-5 and abs(lon - 127.509) < 1e-5


def test_out_of_korea_rejected():
    assert geo.to_wgs84(0, 0, "4326") is None
    assert geo.to_wgs84("", None, "5174") is None


def test_haversine():
    # 위도 0.09도 ≈ 10km
    assert 9.9 < geo.haversine_km(35.0, 127.0, 35.09, 127.0) < 10.1


def test_codes():
    assert disease_code("고병원성 조류인플루엔자(H5N1)") == "HPAI"
    assert disease_code("럼피스킨병") == "LSD"
    assert species_code("육용오리") == "poultry"
    assert species_code("야생조류 분변") == "wild_bird"
    assert species_code("한우") == "cattle"


def test_normalize_mois_farm():
    src = collect.get_source("mois_livestock_farming")
    to5174 = Transformer.from_crs("EPSG:4326", "EPSG:5174", always_xy=True)
    x, y = to5174.transform(127.52, 34.90)
    rec = {"MNG_NO": "A-1", "BPLC_NM": "OO농장", "SALS_STTS_NM": "영업/정상", "PRMN_LSFBIZ_SPECS": "산란계",
           "ROAD_NM_ADDR": "전라남도 순천시 해룡면 ***", "CRD_INFO_X": str(x), "CRD_INFO_Y": str(y)}
    [row] = collect.normalize(src, rec)
    assert row["kind"] == "farm" and row["species"] == "poultry" and row["subspecies"] == "산란계"
    assert row["sigungu"] == "순천시" and abs(row["lat"] - 34.90) < 1e-4


def test_normalize_mafra_outbreak_and_record_finder():
    payload = {"Grid_X": {"totalCnt": 1, "row": [{"OCCRRNC_DE": "20261101", "LKNTS_NM": "고병원성조류인플루엔자",
                                                     "LVSTCKSPC_NM": "육용오리", "CTPRVN_NM": "전남", "SIGNGU_NM": "나주시"}],
                          "result": {"code": "INFO-000"}}}
    recs = collect.find_records(payload)
    [row] = collect.normalize(collect.get_source("mafra_disease_outbreak"), recs[0])
    assert row["date"] == "2026-11-01" and row["disease"] == "HPAI" and row["species"] == "poultry"
    assert collect.total_count(payload) == 1


def test_file_source_and_impact():
    n = collect.run("manual_events")
    assert n == 2
    with db.session() as con:
        ev = db.rows(con, "SELECT id, lat, lon FROM event WHERE kind='wild_detection'")[0]
        # 검출지 기준 2km, 8km, 15km 농장
        farms = []
        for i, km in enumerate((2, 8, 15)):
            farms.append({"uid": f"t{i}", "kind": "farm", "species": "poultry", "name": f"f{km}", "size": 1000 * (i + 1),
                          "lat": ev["lat"] + km / 111.0, "lon": ev["lon"], "source": "test"})
        db.upsert(con, "facility", farms)
    res = analysis.impact(ev["id"])
    assert res["zones"]["control"]["farms"] == 1
    assert res["zones"]["surveillance"]["farms"] == 2
    assert [f["name"] for f in res["farms"]] == ["f2", "f8"]


def test_risk_and_series():
    today = date.today()
    with db.session() as con:
        db.upsert(con, "event", [{"uid": "o1", "kind": "outbreak", "disease": "HPAI", "species": "poultry",
                                  "date": (today - timedelta(days=3)).isoformat(), "sigungu": "순천시",
                                  "lat": 34.95, "lon": 127.51, "source": "test"},
                                 {"uid": "w2", "kind": "wild_detection", "disease": "HPAI", "species": "wild_bird",
                                  "date": (today - timedelta(days=5)).isoformat(), "lat": 34.89, "lon": 127.5, "source": "test"}])
    r = analysis.risk_index("poultry")
    assert r["regions"] and r["regions"][0]["score"] > 0
    s = analysis.weekly_series("poultry", (today - timedelta(days=30)).isoformat())
    assert sum(x["outbreak"] for x in s) == 1 and sum(x["wild_detection"] for x in s) >= 1
    st = analysis.spacetime_pairs("poultry")
    assert st["observed"] >= 1


def test_trace_contacts():
    with db.session() as con:
        db.upsert(con, "contact", [
            {"uid": "c1", "farm_key": "F1", "at": "2026-11-05 10:00", "purpose": "상하차반", "party": "A운송", "source": "t"},
            {"uid": "c2", "farm_key": "F2", "at": "2026-11-07 09:00", "purpose": "상하차반", "party": "A운송", "source": "t"},
            {"uid": "c3", "farm_key": "F3", "at": "2026-10-01 09:00", "purpose": "상하차반", "party": "A운송", "source": "t"}])
    t = analysis.trace_contacts("F1", "2026-11-10")
    assert t["linked_farms"] == ["F2"]


def test_api_smoke():
    from fastapi.testclient import TestClient
    from app.main import app

    c = TestClient(app)
    assert c.get("/config.js").status_code == 200
    assert c.get("/api/summary?species=poultry").json()["kpi"]["wild_detections"] >= 1
    assert c.get("/api/risk?species=poultry").status_code == 200
    src = c.get("/api/sources").json()
    assert src["tables"]["event"] >= 1 and src["tables"]["raw_record"] >= 2
    ev = c.get("/api/events?species=poultry").json()
    assert ev["items"] and ev["freshness"]
    f = {x["source"]: x for x in ev["freshness"]}["manual_events"]
    assert f["data_to"] >= "2026-10-20" and f["collected_at"] and f["refresh"] == "manual" and f["stale"] is False
    risk = c.get("/api/risk?species=poultry").json()
    assert {"freshness", "regions", "missing"} <= set(risk)
    assert any(x["source"] == "manual_events" for x in c.get("/api/freshness").json())
    r = c.post("/api/events", json={"kind": "outbreak", "disease": "구제역", "species": "cattle", "date": "2026-11-02"})
    assert r.status_code == 200
    assert c.get("/").status_code == 200 and c.get("/static/app.js").status_code == 200


def test_stale_flag():
    from app import freshness
    old = "2020-01-01T00:00:00+00:00"
    f = freshness.describe("kma_asos_daily", 1, None, None, old)
    assert f["refresh"] == "daily" and f["stale"] is True
    assert freshness.describe("manual_events", 1, None, None, old)["stale"] is False


# ── 농식품 포털 실제 응답 샘플(2026-10-26 확인)로 매핑 검증 ─────────────────
def test_mafra_outbreak_mapping():
    src = collect.get_source("mafra_disease_outbreak")
    rec = {"ROW_NUM": 3, "ICTSD_OCCRRNC_NO": "00000233", "LKNTS_NM": "뉴캣슬병", "FARM_NM": "박도균",
           "FARM_LOCPLC_LEGALDONG_CODE": "4617043034", "FARM_LOCPLC": "전라남도 나주시 봉황면 덕림리",
           "OCCRRNC_DE": "20030324", "LVSTCKSPC_CODE": "415006", "LVSTCKSPC_NM": "닭-육계",
           "OCCRRNC_LVSTCKCNT": 32200, "DGNSS_ENGN_CODE": "6460117", "DGNSS_ENGN_NM": "전남 축산기술연구소", "CESSATION_DE": ""}
    [row] = collect.normalize(src, rec)
    assert row["date"] == "2003-03-24" and row["disease"] == "ND" and row["species"] == "poultry"
    assert row["subspecies"] == "닭-육계" and row["heads"] == 32200 and row["legal_code"] == "4617043034"
    assert row["sido"] == "전라남도" and row["sigungu"] == "나주시" and row["place"].endswith("덕림리")
    assert "박도균" not in (row["note"] or "")          # 농장주 이름은 표시 필드에 넣지 않음
    assert row["uid"] == collect.make_uid("mafra_disease_outbreak", ["00000233"])


def test_mafra_metric_sources():
    st = collect.normalize(collect.get_source("mafra_disease_stats"),
                           {"QRANT_COMPT_MT": "202012", "CTPRVN_LEGA_CDLDONG": "31", "CTPRVN_NM": "울산광역시",
                            "OCCRRNC_CNT": 4, "OCCRRNC_FRMHS_CNT": 2, "DISE_CD": "1292", "DISE_CD_NM": "저병원성 조류인플루엔자",
                            "LVSTCK_KIND_CD": "415", "LVSTCK_KIND_CD_NM": "닭"})
    assert {r["metric"] for r in st} == {"outbreak_farms:닭:저병원성 조류인플루엔자", "outbreak_heads:닭:저병원성 조류인플루엔자"}
    assert all(r["as_of"] == "2020-12-01" and r["species"] == "poultry" and r["sido"] == "울산광역시" for r in st)

    [fa] = collect.normalize(collect.get_source("mafra_farm_by_area"),
                             {"CTPRVN_NM": "경상남도", "SIGUNGU_NM": "함양군", "EMD_NM": "병곡면", "LVSTCK_NM": "소", "FARM_CO": 30})
    assert fa["emd"] == "병곡면" and fa["species"] == "cattle" and fa["value"] == 30 and fa["as_of"] == ""
    assert fa["metric"] == "farms:소"
    # 같은 읍면동의 닭·오리가 '가금' 한 칸에 겹쳐 쓰이지 않아야 한다
    src = collect.get_source("mafra_farm_by_area")
    chick = collect.normalize(src, {"CTPRVN_NM": "전라남도", "SIGUNGU_NM": "나주시", "EMD_NM": "봉황면", "LVSTCK_NM": "닭", "FARM_CO": 5})
    duck = collect.normalize(src, {"CTPRVN_NM": "전라남도", "SIGUNGU_NM": "나주시", "EMD_NM": "봉황면", "LVSTCK_NM": "오리", "FARM_CO": 7})
    assert chick[0]["species"] == duck[0]["species"] == "poultry" and chick[0]["metric"] != duck[0]["metric"]

    [ds] = collect.normalize(collect.get_source("mafra_disinfection"),
                             {"PSTN_DSNF_PLC_NM": "상주시 분뇨차량 전용 거점소독시설", "ADDR": "경상북도 상주시 낙동면 분황리 0464-0005",
                              "OPER_HR": "09:00 ~ 17:00", "LOT": 128.25909, "LAT": 36.4071})
    assert ds["kind"] == "disinfection" and ds["lat"] == 36.4071 and ds["sigungu"] == "상주시"

    [sl] = collect.normalize(collect.get_source("mafra_slaughter_by_plant"),
                             {"YM": "202608", "CTRD_NM": "충남", "SLAU_PLACE_NM": "중앙산업", "LVSTCKSPC_NM": "말", "THSMON": 11})
    assert sl["place"] == "중앙산업" and sl["date"] == "2026-08-01" and sl["metric"] == "slaughter_heads:말"

    yr = collect.normalize(collect.get_source("mafra_brucella_restriction"),
                           {"YM": "2016", "ADMINIST_ATPT_NM": "대구광역시", "ADMINIST_SIGNGU_NM": "동구",
                            "ADMINIST_EMD_NM": "도평동", "MVMN_LMTT_FRMHS_CO": 4})
    assert yr[0]["as_of"] == "2016" and yr[0]["species"] == "cattle" and yr[0]["emd"] == "도평동"


def test_iterate_values():
    months = collect.iter_values({"kind": "month", "from": "2025-11", "to": "2026-02"})
    assert months == ["202602", "202601", "202512", "202511"]
    assert collect.iter_values({"kind": "year", "from": 2024, "to": 2026}) == ["2026", "2025", "2024"]


def test_all_mafra_sources_have_service_ids():
    for s in collect.load_sources():
        if s["provider"] == "mafra":
            assert s["service_id"].startswith("Grid_"), s["id"]


def test_scheduler_recent_window_and_batch(monkeypatch):
    from app import scheduler

    src = collect.get_source("mafra_disease_stats")
    rec = scheduler._recent(src)
    vals = collect.iter_values(rec["iterate"])
    assert 3 <= len(vals) <= 4 and src["iterate"]["from"] == "2018-01"     # 원본 설정은 그대로
    yr = scheduler._recent(collect.get_source("mafra_brucella_restriction"))
    assert len(collect.iter_values(yr["iterate"])) == 2

    seen = []
    monkeypatch.setattr(collect, "run_source", lambda s, limit=None: seen.append(s["id"]) or 1)
    monkeypatch.setattr(collect, "is_configured", lambda s: s["provider"] in ("mafra", "file"))
    out = scheduler.run_batch("daily")
    assert "mafra_disease_outbreak" in seen and "manual_events" in seen
    assert "mafra_brucella_test" not in seen          # extra 그룹은 기본 자동 수집에서 제외
    assert out["kind"] == "daily"
    with db.session() as con:
        assert con.scalar("SELECT COUNT(*) FROM collect_log WHERE source='auto:daily'") >= 1


def test_auto_api():
    from fastapi.testclient import TestClient
    from app.main import app

    st = TestClient(app).get("/api/auto").json()
    assert st["enabled"] is False and st["at"] == "04:30" and "history" in st


def test_geocode_fallback(monkeypatch):
    from app import geocode
    calls = []

    def fake(client, a):
        calls.append(a)
        return ("OK", 35.0, 127.0) if a == "전라남도 나주시 봉황면" else ("NOT_FOUND", None, None)

    monkeypatch.setattr(geocode, "lookup", fake)
    st, lat, lon, matched = geocode.lookup_with_fallback(None, "전라남도 나주시 봉황면 없는리")
    assert st == "OK" and matched == "전라남도 나주시 봉황면" and calls[0].endswith("없는리")


def test_season_links_ignore_previous_season():
    from app import analysis
    with db.session() as con:
        db.upsert(con, "event", [
            # 2024/25 시즌 (이전 시즌) — 같은 장소라도 연쇄로 묶이면 안 됨
            {"uid": "s0", "kind": "outbreak", "disease": "HPAI", "species": "poultry", "date": "2025-02-10", "lat": 36.00, "lon": 127.00, "source": "t"},
            # 2025/26 시즌
            {"uid": "s1", "kind": "outbreak", "disease": "HPAI", "species": "poultry", "date": "2025-11-01", "lat": 36.00, "lon": 127.00, "source": "t"},
            {"uid": "s2", "kind": "outbreak", "disease": "HPAI", "species": "poultry", "date": "2025-11-10", "lat": 36.09, "lon": 127.00, "source": "t"},   # 10km, 9일 → 연쇄
            {"uid": "s3", "kind": "outbreak", "disease": "HPAI", "species": "poultry", "date": "2025-11-12", "lat": 35.00, "lon": 129.00, "source": "t"},   # 멀리 → 독립
            {"uid": "s4", "kind": "outbreak", "disease": "HPAI", "species": "poultry", "date": "2026-02-01", "lat": 36.01, "lon": 127.00, "source": "t"},   # 30일 넘게 지남 → 독립
            {"uid": "s5", "kind": "outbreak", "disease": "ND", "species": "poultry", "date": "2025-11-11", "lat": 36.05, "lon": 127.00, "source": "t"},     # 다른 질병
        ])
    items = analysis.seasons("poultry", "HPAI")
    assert "2025/26" in [x["label"] for x in items] and items == sorted(items, key=lambda x: -x["year"])
    r = analysis.season_events("poultry", 2025, "HPAI")
    ev = {e["date"]: e for e in r["events"]}
    assert r["index_case"]["date"] == "2025-11-01"
    assert ev["2025-11-01"]["prev"] is None                         # 이전 시즌(2025-02)은 연결하지 않음
    assert ev["2025-11-10"]["linked"] and ev["2025-11-10"]["prev"]["days"] == 9 and 9 < ev["2025-11-10"]["prev"]["km"] < 11
    assert not ev["2025-11-12"]["linked"]
    assert ev["2026-02-01"]["prev"] is None and not ev["2026-02-01"]["linked"]
    assert "2025-11-11" not in ev                                    # 질병 필터
    assert r["summary"]["clusters"] == 1 and r["summary"]["largest_cluster"] == 2
    knox = analysis.spacetime_pairs("poultry", 20, 14, r["from"], r["to"], "HPAI")
    assert knox["observed"] == 1


def test_farm_density_and_risk_fallback():
    with db.session() as con:
        db.upsert(con, "region_stat", [
            {"sido": "충청남도", "sigungu": "홍성군", "emd": "홍북읍", "species": "cattle", "metric": "farms:소", "value": 40,
             "as_of": "", "source": "mafra_farm_by_area"},
            {"sido": "충청남도", "sigungu": "홍성군", "emd": "광천읍", "species": "cattle", "metric": "farms:소", "value": 10,
             "as_of": "", "source": "mafra_farm_by_area"},
            {"sido": "충청남도", "sigungu": "홍성군", "emd": "광천읍", "species": "pig", "metric": "farms:돼지", "value": 7,
             "as_of": "", "source": "mafra_farm_by_area"},
            {"sido": "충청남도", "sigungu": "홍성군", "emd": "홍북읍", "species": "cattle", "metric": "heads:소", "value": 2000,
             "as_of": "", "source": "mafra_farm_by_scale"}])
        db.upsert(con, "geo_cache", [
            {"address": analysis.district_key("충청남도", "홍성군", "홍북읍"), "lat": 36.6, "lon": 126.7, "status": "OK",
             "provider": "vworld-district", "at": db.now()},
            {"address": analysis.district_key("충청남도", "홍성군"), "lat": 36.55, "lon": 126.66, "status": "OK",
             "provider": "vworld-district", "at": db.now()}])
    d = analysis.farm_density("cattle")
    by = {i["emd"]: i for i in d["items"]}
    assert by["홍북읍"]["farms"] == 40 and by["홍북읍"]["lat"] == 36.6
    assert by["홍북읍"]["heads"] == 2000 and by["홍북읍"]["avg"] == 50 and by["광천읍"]["heads"] is None
    assert by["광천읍"]["lat"] == 36.55            # 읍면동 위치가 없으면 시군 대표점
    sg = analysis.farm_density("cattle", "sigungu")["items"]
    assert sg[0]["farms"] == 50
    r = analysis.risk_index("cattle")
    assert r["farm_source"] == "region_stat" and r["regions"][0]["farms"] == 50


def test_district_match():
    from app import geocode

    class R:
        def __init__(self, items): self.items = items
        def raise_for_status(self): pass
        def json(self): return {"response": {"status": "OK", "result": {"items": self.items}}}

    class C:
        def get(self, url, params):
            return R([{"title": "서울특별시 중구 중앙동", "point": {"x": "1", "y": "2"}},
                      {"title": "경상남도 창원시 마산합포구 중앙동", "point": {"x": "128.5", "y": "35.2"}}])

    st, lat, lon = geocode.district(C(), "경상남도", "창원시 마산합포구", "중앙동")
    assert st == "OK" and lat == 35.2
    st, _, _ = geocode.district(C(), "부산광역시", "기장군", "중앙동")
    assert st == "NOT_FOUND"


def test_scheduler_pending(monkeypatch):
    from app import scheduler
    monkeypatch.setattr(collect, "is_configured", lambda s: s["provider"] == "mafra")
    with db.session() as con:
        con.con.execute(db.collect_log.insert().values(source="mafra_farm_by_area", started_at=db.now(),
                                                       finished_at=db.now(), ok=False, rows=0, message="x"))
    assert "mafra_farm_by_area" not in {s["id"] for s in scheduler._pending()}   # 방금 실패한 원천은 12시간 뒤에


def test_scale_source_maps_heads():
    src = collect.get_source("mafra_farm_by_scale")
    st = collect.normalize(src, {"BRD_HEAD_QY": 12585, "CTPRVN_NM": "경상남도", "SIGUNGU_NM": "거제시", "EMD_NM": "거제면",
                                 "LVSTCK_NM": "닭", "FARM_CO": 42})
    assert [(r["metric"], r["value"], r["species"]) for r in st] == [("heads:닭", 12585, "poultry")]


def test_township_shapes_match_old_sido_names():
    import json
    from app import geocode
    sq = lambda x, y: {"type": "MultiPolygon", "coordinates": [[[[x, y], [x + .05, y], [x + .05, y + .05], [x, y + .05], [x, y]]]]}  # noqa: E731
    with db.session() as con:
        db.upsert(con, "region_shape", [
            {"code": "44800250", "sido_cd": "44", "sigungu": "홍성군", "emd": "홍북읍", "full_nm": "충청남도 홍성군 홍북읍",
             "lat": 36.6, "lon": 126.7, "geom": json.dumps(sq(126.7, 36.6)), "at": db.now()},
            {"code": "12810250", "sido_cd": "12", "sigungu": "장흥군", "emd": "관산읍", "full_nm": "전남광주통합특별시 장흥군 관산읍",
             "lat": 34.5, "lon": 126.9, "geom": json.dumps(sq(126.9, 34.5)), "at": db.now()}])
        db.upsert(con, "region_stat", [{"sido": "전라남도", "sigungu": "장흥군", "emd": "관산읍", "species": "cattle",
                                        "metric": "farms:소", "value": 12, "as_of": "", "source": "mafra_farm_by_area"}])
    fc = analysis.farm_shapes("cattle")
    names = {f["properties"]["name"]: f["properties"]["farms"] for f in fc["features"]}
    assert names["충청남도 홍성군 홍북읍"] == 40 and names["전남광주통합특별시 장흥군 관산읍"] == 12   # 옛 시도명도 맞춤
    assert geocode.sido_code("강원도") == "51" and geocode.sido_code("전라북도") == "52"


def test_simplify_keeps_shape():
    from app import geocode
    ring = [[127 + i * 1e-5, 36] for i in range(500)] + [[127.005, 36.01], [127, 36]]
    g = geocode.simplify({"type": "Polygon", "coordinates": [ring]})
    pts = g["coordinates"][0][0]
    assert len(pts) < 10 and pts[0] == pts[-1]


def test_new_datagokr_sources():
    from app import scheduler
    farm = {"BPLC_NM": "샘골농장", "CRD_INFO_X": "175471.943550845", "CRD_INFO_Y": "412890.504438692", "SALS_STTS_NM": "영업/정상",
            "LOTNO_ADDR": "경기도 화성시 만세구 송산면 사강리 ***-**", "MNG_NO": "553000003320260009", "OPN_ATMY_GRP_CD": "5530000",
            "PRMN_LSFBIZ_SPECS": "육계", "LCTN_AREA": ""}
    [f] = collect.normalize(collect.get_source("mois_livestock_farming"), farm)
    assert f["kind"] == "farm" and f["species"] == "poultry" and 37 < f["lat"] < 37.4 and f["sido"] == "경기도"
    [c] = collect.normalize(collect.get_source("qia_farm_entry"),
                            {"구분": "상하차반", "농장 사육축_품종": "닭-육계", "농장 진입목적": "가금 입식", "농장기본 주소": "경상북도 구미시 선산읍 죽장리",
                             "농장명": "선산농장", "업체명": "파인스부화장", "진입시간": "14:14", "진입일자": "2024-01-30"})
    assert c["at"] == "2024-01-30 14:14" and c["party"] == "파인스부화장" and c["purpose"] == "가금 입식"
    [w] = collect.normalize(collect.get_source("qia_crews"), {"구분": "백신접종팀", "업체기본 주소": "대구광역시 북구 동천동", "업체명": "계성축산", "축_품종명": "닭-산란계"})
    assert w["kind"] == "vaccine_team" and w["species"] == "poultry"
    rows = collect.normalize(collect.get_source("kahis_scale_broiler"),
                             {"50000수 이상(호수)": 3, "50000수 이상(수수)": 200000, "년도": 2026, "순번": 1, "시군": "익산", "시도": "전북",
                              "전체두수": 250000, "전체호수": 5, "품종": "육계"})
    ms = {r["metric"]: r["value"] for r in rows}
    assert ms["farms:50000수 이상(호수)"] == 3 and ms["heads:전체두수"] == 250000 and not any("순번" in m for m in ms)
    assert rows[0]["as_of"] == "2026-01-01" and rows[0]["species"] == "poultry"
    # 증분 수집: 성공 기록이 있으면 갱신시점 조건으로 바꾸고 영업상태 조건은 뺀다
    src = collect.get_source("mois_livestock_farming")
    assert scheduler._recent(src) is src
    with db.session() as con:
        con.con.execute(db.collect_log.insert().values(source=src["id"], started_at=db.now(), finished_at=db.now(), ok=True, rows=1, message=""))
    r = scheduler._recent(src)
    assert "cond[SALS_STTS_CD::EQ]" not in r["params"] and len(r["params"]["cond[DAT_UPDT_PNT::GTE]"]) == 14


SAMPLE_VISIT_HTML = """
<td width="10%" align="center" class="list_g_line">2026-10-25 23:57:29</td>
<td width="15%" align="center" class="list_g_line">충청남도 보령시 청라면 황룡리</td>
<td width="15%" align="center" class="list_g_line">농장</td>
<td width="15%" align="center" class="list_g_line">*******</td>
<td width="15%" align="center" class="list_g_line">&nbsp;닭-육계</td>
<td width="15%" align="center" class="list_g_line">가축운반</td>
<td width="15%" align="center" class="list_g_line">*******</td> </tr>
<tr><td class="list_g_line">2026-10-25 08:10:00</td><td class="list_g_line">충청남도 보령시 청라면 황룡리</td>
<td class="list_g_line">농장</td><td class="list_g_line">*******</td><td class="list_g_line">닭-육계</td>
<td class="list_g_line">가축운반</td><td class="list_g_line">*******</td></tr>
<tr><td class="list_g_line">2026-10-25 09:00:00</td><td class="list_g_line">경상북도 포항시 북구 흥해읍 남송리</td>
<td class="list_g_line">농장</td><td class="list_g_line">*******</td><td class="list_g_line">소-한우</td>
<td class="list_g_line">사료운반</td><td class="list_g_line">*******</td></tr>
"""


def test_vehicle_visits_parse_aggregate_and_near():
    from app import kahis_visit
    rows = kahis_visit.parse(SAMPLE_VISIT_HTML)
    assert len(rows) == 3 and rows[0][3] == "닭-육계"
    agg = {r["address"]: r for r in kahis_visit.aggregate("2026-10-25", rows)}
    br = agg["충청남도 보령시 청라면 황룡리"]
    assert br["n"] == 2 and (br["first_hour"], br["last_hour"]) == (8, 23)
    assert kahis_visit._split("경상북도 포항시 북구 흥해읍 남송리")["sigungu"] == "포항시 북구"
    kahis_visit.save_day("2026-10-25", rows)
    kahis_visit.save_day("2026-10-25", rows)          # 같은 날 다시 받아도 중복되지 않음
    with db.session() as con:
        assert con.scalar("SELECT SUM(n) FROM vehicle_visit WHERE date = ?", [date(2026, 10, 25)]) == 3
        assert con.scalar("SELECT species FROM visit_kind WHERE species_raw = '닭-육계'") == "poultry"
        db.upsert(con, "event", [{"uid": "vv1", "kind": "outbreak", "disease": "HPAI", "species": "poultry", "date": "2026-10-30",
                                  "place": "충청남도 보령시 청라면 황룡리", "lat": 36.4, "lon": 126.6, "source": "test"}])
        eid = con.scalar("SELECT id FROM event WHERE uid='vv1'")
    v = analysis.visits_near_event(eid, 3, 21)
    assert v["total"] == 2 and v["same_place"] == 2 and v["by_vehicle"][0] == ("가축운반", 2) and v["days_covered"] == 1
    m = analysis.visit_map("poultry", 7)
    assert m["unlocated"] == 1
    st = kahis_visit.backfill_status()
    assert st["days_done"] == 1 and st["visits"] == 3


def test_explore_endpoints():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    cat = c.get("/api/explore").json()["sources"]
    assert any(s["id"] == "mafra_disease_outbreak" for s in cat)
    t = c.get("/api/explore/manual_events/table?limit=5").json()
    assert t["table"] == "event" and "date" in [x["name"] for x in t["columns"]]
    t2 = c.get("/api/explore/mafra_farm_by_area/table?f_species=cattle&sort=value&desc=1").json()
    assert t2["total"] >= 1 and all(r["species"] == "cattle" for r in t2["items"])
    f = c.get("/api/explore/mafra_farm_by_area/facet/species").json()
    assert f["distinct"] >= 1
    assert c.get("/api/explore/mafra_farm_by_area/facet/drop_table").status_code == 404
    v = c.get("/api/explore/kahis_vehicle_visit/table?f_vehicle=가축운반").json()
    assert v["total"] >= 1
    tl = c.get("/api/explore/kahis_vehicle_visit/timeline").json()
    assert tl["unit"] == "day" and tl["items"][0]["n"] >= 2
    pr = c.get("/api/explore/mafra_farm_by_area/profile").json()
    assert pr["total"] >= 1 and pr["columns"][0]["name"] == "as_of"
    csv = c.get("/api/explore/mafra_farm_by_area/csv?f_species=cattle").text
    assert csv.startswith("﻿as_of,") and "홍북읍" in csv
    with db.session() as con:
        db.save_raw(con, "mafra_disease_outbreak", "t1", [{"FARM_NM": "홍길동", "TELNO": "010-1", "LKNTS_NM": "HPAI"}])
    r = c.get("/api/explore/mafra_disease_outbreak/raw?q=HPAI").json()
    p = r["items"][0]["payload"]
    assert p["FARM_NM"] == "●●●" and p["TELNO"] == "●●●" and p["LKNTS_NM"] == "HPAI"
    assert c.get("/api/explore/nope/table").status_code == 404


def test_history_endpoints():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    for k in ("seasons", "regions", "months", "repeats", "diseases", "visits"):
        r = c.get(f"/api/history/{k}?species=poultry&disease=HPAI")
        assert r.status_code == 200, (k, r.text[:200])
    s = c.get("/api/history/seasons?species=poultry&disease=all").json()
    assert s["rows"] and all(len(x["cum"]) >= 1 for x in s["series"]) and "by_now" in s["rows"][0]
    m = c.get("/api/history/months?species=poultry").json()
    assert len(m["month_total"]) == 12
    v = c.get("/api/history/visits?species=poultry").json()
    assert v["coverage"] and v["coverage"]["days"] >= 1
    assert c.get("/api/history/nope?species=poultry").status_code == 404
    assert c.get("/api/history/seasons?species=dog").status_code == 400


GOOGLE_RSS = """<?xml version="1.0" encoding="UTF-8"?><rss><channel><title>t</title>
<item><title>김포 한강 하류서 야생조류 고병원성 조류인플루엔자 검출 - 매일경제</title><link>https://news.google.com/a1</link>
<pubDate>Tue, 27 Oct 2026 03:12:11 GMT</pubDate><description>&lt;a href="x"&gt;요약&lt;/a&gt;</description><source url="https://mk.co.kr">매일경제</source></item>
<item><title>김포 한강 하류서 야생조류 고병원성 조류인플루엔자 검출 - 동아일보</title><link>https://news.google.com/a2</link>
<pubDate>Tue, 27 Oct 2026 01:00:00 GMT</pubDate><source url="https://donga.com">동아일보</source></item>
</channel></rss>"""
MAFRA_RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>RSS 게시판 2.0</title>
<item><title>소 럼피스킨 백신 추가 접종 실시</title><link>http://www.mafra.go.kr/bbs/home/792/1/artclView.do</link><pubDate>2026-10-27 12:22:33.846</pubDate><description>방역</description></item>
<item><title>한식대축제 성료</title><link>http://www.mafra.go.kr/bbs/home/792/2/artclView.do</link><pubDate>2026-10-27 08:43:59.788</pubDate></item>
</channel></rss>"""


def test_news_monitor(monkeypatch):
    from app import news

    class R:
        def __init__(self, t): self.text = t
        def raise_for_status(self): pass

    class C:
        def get(self, url, **k):
            return R(GOOGLE_RSS if "news.google" in url else MAFRA_RSS)

    monkeypatch.setattr(news.time, "sleep", lambda s: None)
    n1 = news.fetch_news(C())
    assert n1 == 1                                   # 같은 기사(언론사만 다름)는 하나로
    assert news.fetch_news(C()) == 0                 # 다시 받아도 중복 저장 없음
    n2 = news.fetch_notices(C())
    assert n2 == 2 * len(news.NOTICE_FEEDS) or n2 == 2   # 피드마다 같은 내용이면 링크가 같아도 원천이 달라 따로
    r = news.query("notice", relevant=True)
    titles = [i["title"] for i in r["items"]]
    assert "소 럼피스킨 백신 추가 접종 실시" in titles and "한식대축제 성료" not in titles
    assert all(i["link"].startswith("https://") for i in r["items"])
    nz = news.query("news")["items"][0]
    assert nz["title"].endswith("검출") and "poultry" in nz["species"]
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).get("/api/news?kind=news").json()["total"] == 1


def test_history_same_week_and_weather_latest():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    s = c.get("/api/history/seasons?species=poultry&disease=all").json()
    assert "now_week" in s and s["current"]
    for r_, ser in zip(s["rows"], s["series"]):
        assert r_["by_now"] == ser["cum"][min(s["now_week"], len(ser["cum"]) - 1)]
    with db.session() as con:
        db.upsert(con, "context_ts", [{"place": "119:수원", "date": "2026-10-26", "metric": "temp_avg", "value": 9.5, "source": "kma_asos_daily"}])
    w = c.get("/api/weather/latest").json()
    assert w["items"][0]["name"] == "수원" and w["items"][0]["temp_avg"] == 9.5


def test_bird_tracks_vectors(tmp_path, monkeypatch):
    from app import birds
    assert birds.compass16(0)[0] == "북" and birds.compass16(45)[0] == "북동" and birds.compass16(350)[0] == "북"
    assert birds.clock(0) == 12 and birds.clock(90) == 3 and birds.clock(40) == 1
    b = birds.bearing(37.27, 127.44, 42.90, 129.50)          # 이천 → 옌볜: 북북동
    assert 10 < b < 30 and birds.compass16(b)[0] == "북북동"
    assert birds.parse_date("‘23.3.13", 2023).isoformat() == "2023-03-13"
    assert birds.locate("중국 지린성 판시")[3] == "중국 지린성 판시"
    lat, lon, prec, key = birds.locate("중국 지린성 어딘가")
    assert key == "중국 지린성" and prec == "province"
    f = tmp_path / "철새이동정보.csv"
    f.write_text("구분,연도,북상일,조류종(부착코드),추적기코드,북상전 국내 위치,북상후 국외 위치,,\n"
                 "1,2023년,‘23.3.13,큰기러기,nwd21085,경기 김포,중국 지린성 옌볜,,\n"
                 "2,2023년,‘23.3.19,청둥오리,br22090,충남 논산,북한,,\n", encoding="utf-8")
    r = birds.load(f)
    assert r["rows"] == 2 and not r["missing"]
    q = birds.query(2023)
    k = {x["tag"]: x for x in q["items"]}
    assert k["nwd21085"]["compass"] in ("북북동", "북동") and k["nwd21085"]["distance_km"] > 500
    assert k["br22090"]["dest_precision"] == "country" and sum(q["rose"]) == 2
    assert birds.load(f)["rows"] == 2      # 다시 넣어도 중복 없음
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).get("/api/birds?year=2023").json()["items"][0]["species"]


def test_explore_every_source_opens():
    """sources.yaml 의 모든 원천이 데이터 조회 화면에서 표·요약·기간 그래프로 열려야 한다."""
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    for s in c.get("/api/explore").json()["sources"]:
        for ep in ("table?limit=3", "profile", "timeline"):
            r = c.get(f"/api/explore/{s['id']}/{ep}")
            assert r.status_code == 200, (s["id"], ep, r.text[:200])
    b = c.get("/api/explore/migratory_birds/table").json()
    assert b["table"] == "bird_track"


def test_assoc_recovers_planted_effects():
    """밀집도·주변 발생 효과를 심은 합성 자료에서 연관 분석이 그 방향을 되찾는지(질병 IB로 다른 테스트와 분리)."""
    import math
    import random
    from app import assoc
    rnd = random.Random(3)
    now = db.now()
    regs, rs, gc, evs = [], [], [], []
    for i in range(40):
        g = {"sido": "충청북도", "sigungu": f"연관{i:02d}군", "lat": rnd.uniform(35.0, 37.5), "lon": rnd.uniform(126.8, 128.8),
             "farms": int(math.exp(rnd.gauss(3.0, 0.9))) + 2}
        regs.append(g)
        rs.append({"sido": g["sido"], "sigungu": g["sigungu"], "emd": "가면", "species": "poultry", "metric": "farms:닭",
                   "value": g["farms"], "as_of": "2026-09", "source": analysis.FARM_SOURCE, "collected_at": now})
        gc.append({"address": analysis.district_key(g["sido"], g["sigungu"]), "lat": g["lat"], "lon": g["lon"],
                   "status": "OK", "provider": "test", "at": now})
    uid = 0
    obs = []
    for y in range(2020, 2025):
        d = date(y, 10, 1)
        while d < date(y + 1, 4, 1):
            for g in regs:
                h = 0.0004 * g["farms"] ** 0.8
                if any(1 <= (d - od).days <= 14 and geo.haversine_km(g["lat"], g["lon"], la, lo) <= 30 for od, la, lo in obs[-40:]):
                    h *= 3
                if rnd.random() < h:
                    uid += 1
                    obs.append((d, g["lat"], g["lon"]))
                    evs.append({"uid": f"assoc{uid}", "kind": "outbreak", "disease": "IB", "species": "poultry", "date": d,
                                "sido": g["sido"], "sigungu": g["sigungu"], "place": f"{g['sido']} {g['sigungu']} 가면 가리",
                                "lat": g["lat"], "lon": g["lon"], "source": "test", "collected_at": now})
            d += timedelta(days=1)
    with db.session() as con:
        db.upsert(con, "region_stat", rs)
        db.upsert(con, "geo_cache", gc)
        db.upsert(con, "event", evs)
    r = assoc.compute("poultry", "IB")
    assert r["model"]["available"], r["model"]
    coef = {c["factor"]: c for c in r["model"]["models"][0]["coef"]}
    assert coef["density"]["or"] > 1.3 and coef["density"]["sig"]
    assert coef["neighbor_outbreak"]["or"] > 1.3
    assert r["model"]["pred"] and 0 < r["model"]["pred"][0]["p"] < 1
    dens = {f["factor"]: f for f in r["density"]["factors"]}
    assert dens["farms"]["rows"][4]["rr"] > dens["farms"]["rows"][0]["rr"]
    assert r["wild"]["available"] is False or len(r["wild"]["grid"]) == 3
    assert r["temp"]["available"] is False and r["visits"]["available"] is False
    # API: 처음엔 계산 중, 끝나면 결과
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    d = c.get("/api/assoc?species=poultry&disease=IB").json()
    assert d["status"] in ("computing", "ready", "updating")
    assert assoc.get("poultry", "IB", wait=60)["status"] == "ready"
    assert c.get("/api/assoc?species=all").json()["status"] == "na"


def _hwpx(path, rows, spans=()):
    """테스트용 최소 hwpx: 표 하나(병합 셀 포함)."""
    import zipfile
    ns = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'
    tcs = []
    for r, row in enumerate(rows):
        for c, txt in enumerate(row):
            if txt is None:
                continue
            rs = dict(spans).get((r, c), 1)
            tcs.append(f'<hp:tc><hp:subList><hp:p><hp:run><hp:t>{txt}</hp:t></hp:run></hp:p></hp:subList>'
                       f'<hp:cellAddr colAddr="{c}" rowAddr="{r}"/><hp:cellSpan colSpan="1" rowSpan="{rs}"/></hp:tc>')
    xml = f'<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" {ns}><hp:p><hp:run><hp:tbl><hp:tr>{"".join(tcs)}</hp:tr></hp:tbl></hp:run></hp:p></hs:sec>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/hwp+zip")
        z.writestr("Contents/section0.xml", xml)


def test_wild_file_import(tmp_path):
    from app import wild
    f = tmp_path / "'22~'23년 동절기 야생조류 고병원성 AI 발생현황.hwpx"
    _hwpx(f, [["연번", "시료채취일", "확진일", "시도", "시군", "지점", "종", "혈청형"],
              ["1", "10.10", "10.17", "충남", "천안시", "봉강천", "청둥오리", "H5N1"],
              ["2", "11.28", "12.2", "전남", None, "영암호", "큰고니", "H5N1"],     # 시군 병합(위 칸과 같음)은 아래에서 따로
              ["3", "1.5", "1.9", "경기", "화성시", "남양 우정면", "흰뺨검둥오리", "H5N1"],
              ["4", "2.1", "2.3", "경기", "화성시", "매향리", "저병원성", "H5N3 저병원성"]],
          spans=[((1, 4), 2)])
    rows = wild.rows_from_tables(wild.file_tables(f), wild.season_year(f.name))
    assert [(r["date"].isoformat(), r["sigungu"], r["place"]) for r in rows] == [
        ("2022-10-17", "천안시", "봉강천"), ("2022-12-02", "천안시", "영암호"), ("2023-01-09", "화성시", "남양 우정면")]
    assert rows[0]["sampled"].isoformat() == "2022-10-10"
    assert wild.parse_date("'22.11.10.(목)", None).isoformat() == "2022-11-10"
    assert wild.season_year("2021~2022 야생") is None or wild.season_year("2021~2022 야생") == 2021
    r = wild.load([f])                                  # 브이월드 키가 없으면 좌표 없이 들어감
    assert r["rows"] == 3 and r["files"][0]["rows"] == 3
    with db.session() as con:
        assert con.scalar("SELECT COUNT(*) FROM event WHERE source = 'wild_files'") == 3


def test_weather_backfill_plans_only_outbreak_months(monkeypatch):
    from app import collect, weather
    rs = weather.needed_ranges()
    assert rs and all((b - a).days <= 900 for a, b in rs)
    calls = []

    def fake(src, base, limit=None):
        p = src["params"]
        calls.append(p["stnIds"])
        a = date(int(p["startDt"][:4]), int(p["startDt"][4:6]), int(p["startDt"][6:]))
        b = date(int(p["endDt"][:4]), int(p["endDt"][4:6]), int(p["endDt"][6:]))
        d = a
        while d <= b:
            yield {"tm": d.isoformat(), "stnId": p["stnIds"], "avgTa": "1.5"}
            d += timedelta(days=1)
    monkeypatch.setattr(collect, "fetch_paged", fake)
    n = len(weather.plan())
    assert n > 0
    weather.run(pause=0)
    assert len(calls) == n and weather.plan() == []      # 받은 구간은 다시 받지 않음
