# 가축질병 추적 시스템 (livestock-disease-tracker)

농림축산식품부·공공데이터포털의 공개 데이터를 모아 닭(가금)·돼지·소의 질병 발생을 국토교통부 브이월드(VWorld) 지도 위에서 추적하는 시스템입니다. 발생·야생 검출·농장·시설·기상 데이터를 한 저장소에 정규화해 두고, 방역대 영향, 접촉 역추적, 시공간 연쇄, 위험지수를 계산합니다.

기획서: 「가축질병 추적 시스템 기획서 (닭·돼지·소)」 문서 참고.

## 빠른 시작 (macOS)

```bash
cd ~/Desktop/devs/livestock-disease-tracker
cp .env.example .env          # 키 입력: DATA_GO_KR_KEY, MAFRA_KEY, VWORLD_KEY, DATABASE_URL
./run.sh                      # Postgres(도커) 기동 → venv·패키지 → 테이블 생성 → 수기 이벤트 적재 → 서버 실행
```

## 농림부 데이터 수집 (농식품 빅데이터 포털)

신청 완료 36종 중 **27종을 서비스 ID·실제 응답 필드까지 확인해 `config/sources.yaml` 에 넣었습니다**(2026-10-26 기준).

```bash
./collect.sh          # 핵심(group:core): 발생정보 4.6만 건(1995~현재), 월별 질병통계, 거점소독시설 210곳, 농장 통계, 도축실적
./collect.sh extra    # 추가: 브루셀라 검사(약 35만 건)·이동제한, 보험, 분뇨, 폐업지원, 인공수정사 등
```

- 수집 후 화면 「데이터 관리」에서 원천별 **보기**를 누르면 정규화된 표를 검색·페이지 넘기며 볼 수 있습니다. 원본 응답은 Postgres `raw_record` 에 그대로 남습니다.
- 발생 정보의 소재지는 '리' 단위라 브이월드 주소검색으로 **리 대표점 좌표**를 붙입니다. 리로 못 찾으면 읍면동 → 시군구 대표점으로 한 단계씩 넓힙니다. 자동 수집은 발생 정보를 받은 직후 최근 발생부터 변환합니다(`python -m app.cli geocode`, 결과는 `geo_cache`).
- 농장명(농장주 이름)은 원본에만 보관하고 화면·분석 필드에는 넣지 않습니다.
- **데이터 시점**(2026-10-26 첫 수집): 가축 질병 발생 정보는 1995-06 ~ 2026-10-22로 현행 갱신됩니다. 질병발생 통계는 ~2024-12, 브루셀라 이동제한은 ~2016에서 멈춰 있습니다. 야생조류·야생멧돼지 검출은 이 포털에 없어 `data/files/manual_events.csv`(보도자료)로 넣습니다. 화면의 "데이터 기준"에 기간이 표시됩니다.
- 꺼 둔 원천: 축산차량 통계·축산관계시설 통계(필수 파라미터 마감일자 값 미확인), 인공수정소 현황(이름 검색 필수).
- 링크형 7종(한우 인공수정이력, 소·쇠고기 개체정보, DNA 동일성검사, 계란 등급판정, 등급판정확인서, 팜맵 기상·토양)은 각 기관 API라 별도 신청이 필요합니다. 동물등록(반려동물)은 범위 밖이라 제외했습니다.

## 자동 수집 (하루 한 번)

`./run.sh` 로 서버를 켜 두면 수집을 따로 돌리지 않아도 됩니다.

- **처음 켤 때** 농림부 핵심 데이터가 비어 있으면 백그라운드에서 전체 수집을 시작합니다(수십 분 걸릴 수 있음, 화면은 바로 사용 가능).
- **매일 04:30**(`AUTO_COLLECT_AT`)에 최신화합니다. 월별 원천은 최근 3개월, 연도별은 최근 2년만 다시 받고, 새 발생 주소는 좌표로 바꿉니다.
- 그 시각에 맥이 잠자기 중이었으면 깨어난 뒤 5분 안에 그날 몫을 실행합니다. 서버가 꺼져 있으면 수집도 멈춥니다.
- 「데이터 관리 › 자동 수집」에서 다음 실행 시각·진행 상태·최근 결과를 보고, **지금 최신화**로 바로 돌릴 수 있습니다.
- 끄려면 `.env` 에 `AUTO_COLLECT=0`. 추가 원천까지 자동으로 받으려면 `AUTO_COLLECT_GROUPS=core,extra`.
- 수동 실행(`./collect.sh`)도 그대로 쓸 수 있습니다.

## 통합 상황판: 현황 지도

- **농장 규모(초록 면)**: 공개 데이터에는 개별 농장 위치가 없어 읍면동 경계(브이월드 `LT_C_ADEMD_INFO`, 단순화해 `region_shape` 에 보관)를 칠합니다. 색 기준은 사육두수/농장 수/농장당 평균 사육두수 중 선택, 마우스를 올리면 읍면동 이름·농장 수·사육두수·평균이 나옵니다. 경계를 받기 전에는 읍면동 대표점(행정구역 중심 — 면사무소 근처일 수 있음, 농장 위치 아님)에 원으로 표시합니다. 농림부 자료의 옛 시도명(전라남도·강원도·전라북도)은 시도 코드로 맞춥니다.
- **이번 시즌 발생**(진한 점, 최근 30일은 검은 테두리) · **직전 시즌 발생**(흐린 점, 참고용) · **방역대**(최근 30일 발생 주변 3km/10km) · **거점소독시설** — 지도 위 체크박스로 켜고 끕니다.
- 위험 상위 시군: 농장 좌표가 없으면 읍면동 농장 통계를 시군으로 묶어 밀집도·야생 근접·최근 발생을 계산합니다(규모 요인은 제외).
- KPI 옆 '직전 시즌 같은 기간' 수치로 이번 시즌이 빠른지 느린지 봅니다.
- 업데이트로 비워진 원천은 서버를 켤 때 바로 다시 받습니다(자동 수집 기록의 '빈 원천 채우기').

## 뉴스·공지 (메뉴 '뉴스·공지')

- 뉴스: 구글 뉴스 RSS 검색(최근 7일, 축종별 검색어) — 5분마다(`NEWS_EVERY_SEC`). 같은 제목은 하나로 묶음
- 공지: 농식품부 게시판 RSS(공지·공고 791 / 보도자료 792 / 설명자료 793) — 30분마다(`NOTICE_EVERY_SEC`). 가축 방역 관련 단어가 있으면 '관련'
- 상황판 오른쪽에 최근 소식, 메뉴에 새 소식 수 배지(1분마다 확인). 끄기: `NEWS_MONITOR=0`

## 철새 이동 (검역본부 위치추적 북상 기록)

- `data/files` 에 이름에 '철새'가 들어간 csv/xlsx 를 넣으면 서버 시작 때(또는 데이터 관리 '수집') `bird_track` 으로 넣음
- 가공: 출발(북상 전 국내 위치)·도착(북상 후 국외 위치) 지명 → 좌표(`app/birds.py` 지명 사전, 시·군/성·주/나라 대표점과 정확도) → 거리(km)·방위각(°)·16방위(북북동 등)·시계 방향(1~12시)
- 상황판 '철새 이동(북상)' 레이어: 종별 색 화살표(출발→도착), 연도·종 선택. 역대 분석: 방향 분포(16방위)·연도별 북상 시기
- API: `/api/birds?year=&species=`, `POST /api/birds/load`

## 지도 표현

- 상황판 '관측소 기온': 기상청 ASOS 관측소(전국 57곳, 위치는 `app/stations.py`)의 최근 일평균 기온을 색 점으로(범례 7구간)
- 역대 분석 '시즌 전파 연결망 비교': 두 시즌을 회색 지도 위에 나란히, 연쇄 가능으로 이어진 발생 쌍을 붉은 선으로

## 역대 분석 (메뉴 '역대 분석')

- 시즌별 누적 발생 곡선(시즌 시작 후 주차) — 이번 시즌(빨강)·지난 시즌(초록)·이전 시즌(회색). 표의 '같은 시점까지'로 이번 시즌 속도를 비교, 행을 누르면 그 시즌 재생으로 이동
- 연도 × 월 히트맵(계절성), 질병 × 연도 히트맵(축종 안 모든 질병)
- 반복 발생 지역(시군 × 시즌), 여러 시즌에 반복 발생한 마을(리)
- 발생 전 21일 축산차량 방문: 발생 리의 차량유형별 하루 방문 수 ÷ 같은 날 다른 리 평균(배율). 관찰된 연관이며 인과 판단은 아님
- API: `/api/history/{seasons|regions|months|diseases|repeats|visits}?species=&disease=` (결과는 데이터가 바뀔 때까지 메모리에 보관)

## 데이터 조회 (메뉴 '데이터 조회')

- 원천(API)별 정규화 데이터 표(검색·기간·항목 필터·정렬·CSV), 원본 API 응답(사람 이름·연락처 항목은 가림), 항목 요약(채움 비율·고유값·최소/최대·값 분포)
- API: `/api/explore`, `/api/explore/{id}/table|facet/{col}|timeline|profile|raw|csv`

## 축산차량 시설방문 (KAHIS)

- 공공데이터포털 '축산차량 방문정보'(농림축산검역본부, LINK형)의 공개 조회 화면을 화면과 같은 순서(조회 화면 → 지역 목록 → 목록)로 조회합니다. 시설명·차량번호는 원본에서 가려져 있어 받지 않습니다.
- 하루 전국 약 17만 건(원본 140MB 안팎, 시도 16번 조회, 약 5분). (날짜, 리, 시설구분·축종·차량유형)별 횟수로 묶어 저장합니다(`vehicle_visit` + 번호표 `visit_place`, `visit_kind`).
- **별도 수집기**로 받습니다(웹 서버와 따로, 다른 터미널에서): `./visits.sh`
  - 계속 실행: 시작할 때와 매일 04:30에 어제·오늘분, 그 사이에는 최근 날짜부터 3년 전(`--years`, `VISIT_YEARS`)까지 빠진 날을 채움(시도·날짜 사이 1초)
  - `./visits.sh --status` 현황, `--probe` 접속 확인, `--day 2026-10-25` 하루, `--from … --to …` 기간, `--once` 한 번만
  - 받은 날은 `vehicle_visit_day` 에 남아 멈췄다 다시 켜도 이어서 받음. 로그는 `data/logs/visits.log`. 맥에서는 실행 중 잠자기를 막음(caffeinate)
- 진단(웹 서버): `/api/visits/probe`

## 발생 추적: 시즌 재생과 확산 그래프

- **시즌 단위**: 가금 HPAI는 9월~이듬해 8월을 한 시즌(예: 2025/26)으로, 돼지·소는 달력 연도로 나눕니다(`app/species.py` 의 `season_start_month`). 연관 분석은 같은 시즌 안에서만 합니다 — 1년 전 발생과는 연결하지 않습니다.
- **연쇄 판단**: 각 발생마다 같은 시즌의 앞선 30일(소 21일) 안 발생 중 가장 가까운 곳을 찾고, 30km(소 20km) 이내면 "연쇄 가능", 아니면 "독립·원거리(새 유입 가능)"로 표시합니다. 연쇄로 이어진 발생은 묶음(#번호)으로 묶습니다. 기준값은 `link` 설정과 `/api/season?link_days=&link_km=` 로 바꿀 수 있습니다.
- **재생**: 시즌·질병을 고르고 ▶ 를 누르면 그 시점까지의 발생이 지도에 계속 쌓입니다(누적). '새 발생 강조' 기간(3/7/14/30일) 안의 발생은 크게·테두리로, 방금 나타난 발생은 바깥 고리로 표시합니다. 연쇄 가능한 발생은 선행 발생과 선으로 이어지고 선도 남습니다. 3주 넘게 조용한 기간은 건너뛸 수 있고, 진행 중인 시즌은 오늘까지만 재생합니다.
- **시간–거리 그래프**: 가로 날짜, 세로 "가장 가까운 선행 발생까지 거리(로그)" 또는 "지표 사례로부터 거리". 아래쪽에 모인 빨간 점 = 주변으로 번지는 중, 위쪽 보라 점 = 멀리서 새로 튄 발생.
- **시즌 발생 곡선**: 주별 신규(막대)·누적(선). 두 그래프의 세로선이 재생 위치이고, 점·막대를 누르면 그 시점으로 이동합니다.
- 연관 분석 화면의 Knox 시공간 군집과 주간 추이도 선택한 시즌·질병 기준입니다.

## 저장소 (PostgreSQL)

`docker-compose.yml` 의 전용 컨테이너 `ldt-db`(postgres:16, **포트 5433**)를 씁니다. 기존 `woori-db`(5432)와 겹치지 않습니다.

```bash
docker compose up -d                       # run.sh 가 자동으로 실행
# .env
DATABASE_URL=postgresql+psycopg://ldt:ldt@localhost:5433/ldt
```

기존 woori-db 안에 데이터베이스만 따로 만들어 쓰려면 `CREATE DATABASE ldt;` 후 `DATABASE_URL=postgresql+psycopg://<사용자>:<비밀번호>@localhost:5432/ldt` 로 바꾸면 됩니다. `DATABASE_URL` 을 비우면 SQLite 파일로 동작합니다.

수집할 때마다 두 곳에 저장됩니다.

| 테이블 | 내용 | 용도 |
| --- | --- | --- |
| `raw_record` | API·파일에서 받은 원본 레코드 그대로(JSONB), 수집 묶음(`batch`)·시각 포함 | 매핑을 바꾼 뒤 재정규화, 보고서 원천 증빙 |
| `event` `facility` `contact` `context_ts` `region_stat` | 정규화된 분석용 데이터, 행마다 `collected_at` | 화면·분석·보고서 |
| `collect_log` | 원천별 수집 시도·성공·실패 메시지 | 최신성 표시 |

보고서·분석용 예시 쿼리:

```sql
-- 축종·질병별 월간 발생
SELECT date_trunc('month', date) AS month, species, disease, COUNT(*)
FROM event WHERE kind = 'outbreak' GROUP BY 1, 2, 3 ORDER BY 1;

-- 원본 응답에서 특정 필드 꺼내기
SELECT payload->>'BPLC_NM', payload->>'DTL_SALS_STTS_NM'
FROM raw_record WHERE source = 'mois_livestock_farming' ORDER BY fetched_at DESC LIMIT 20;
```

## 데이터 최신성 표시

모든 조회 API는 결과와 함께 `freshness` 를 돌려주고, 화면은 카드마다 아래에 **데이터 기준**을 표시합니다.

- 원천 이름, 데이터 자체의 기간(발생일·관측일 기준), DB에 적재된 시각
- `sources.yaml` 의 `refresh`(daily/weekly/monthly/yearly/manual) 주기의 2배가 지나도록 다시 수집되지 않으면 **갱신 지연**
- 상단 바에 최종 수집 시각과 지연된 원천 수, 「데이터 관리」에 원천별 기간·최근 수집·결과
- `GET /api/freshness` 로 전체 현황 조회

## 언어

오른쪽 위 「한국어 / English」로 화면 문구, 축종·질병 이름, 날짜 형식이 바뀝니다(선택은 브라우저에 기억). 지명·비고 같은 원천 데이터 값은 원문 그대로 표시합니다. 문구는 `web/i18n.js` 에 있습니다.

브라우저에서 http://localhost:8000 을 엽니다. 브이월드 키가 없으면 대체 지도로 뜹니다.

### 배경지도: 브이월드(VWorld)

- 국토교통부 [브이월드](https://www.vworld.kr) WMTS 타일을 Leaflet으로 띄웁니다. 오른쪽 위에서 일반 / 회색 / 위성(+하이브리드)을 바꿀 수 있습니다.
- `.env` 의 `VWORLD_KEY` 에 인증키를 넣습니다. 브이월드 인증키 관리에서 키의 **서비스 URL**에 `http://localhost`(운영 시 실제 도메인)가 등록돼 있어야 타일이 뜹니다.
- 타일이 하나도 안 오면 지도 위에 확인 안내가 뜹니다. 개발키는 3개월 기한(최대 3회 연장)이니 운영 전에 운영키로 바꾸세요.

## 데이터 연결 순서

1. **원천 설정** — `config/sources.yaml` 에서 비어 있는 `endpoint`/`service_id` 를 채웁니다.
   - 공공데이터포털: 마이페이지 › 오픈API › 해당 API › End Point + 상세기능 오퍼레이션 이름
   - 농식품 빅데이터 포털: 데이터 상세 › OpenAPI 탭 샘플 URL 의 서비스 ID (`Grid_…`)
2. **필드 확인** — 원본 응답을 DB에 넣지 않고 봅니다.
   ```bash
   .venv/bin/python -m app.cli sources                     # ● 설정됨 / ○ 미설정
   .venv/bin/python -m app.cli probe mois_livestock_farming 3
   ```
   `fields` 목록에서 실제 컬럼명을 찾아 `sources.yaml` 의 `map` 후보 맨 앞에 넣습니다. `normalized` 에 값이 제대로 들어오면 끝입니다.
3. **수집**
   ```bash
   .venv/bin/python -m app.cli collect mois_livestock_farming   # 하나
   .venv/bin/python -m app.cli collect all                      # 설정된 것 전부
   ```
   화면의 「데이터 관리」에서도 원천별로 수집 버튼을 누를 수 있습니다. 원본 레코드는 DB `raw_record` 에 남습니다(`KEEP_RAW_FILES=1` 이면 `data/raw/` 에 JSON 파일도).

### API가 없는 데이터

| 데이터 | 넣는 방법 |
| --- | --- |
| 철새 이동 정보 (검역본부, xlsx) | 내려받아 `data/files/철새이동정보.xlsx` 로 저장 → `collect migratory_birds` |
| 야생조류 HPAI 검출 (보도자료) | 화면 「데이터 관리 › 이벤트 직접 입력」 또는 `data/files/manual_events.csv` 에 한 줄 추가 → `collect manual_events` |
| 럼피스킨·HPAI·ASF 발생현황 정보공개 (HWP/PDF) | 위와 같음 |

`manual_events.csv` 에는 2026-10-20 순천만 야생조류 H5형 검출과 위기경보 '심각' 상향이 들어 있습니다(좌표는 순천만습지 대표점, 근사값).

## 구조

```
app/
  settings.py   .env 로딩, 경로
  db.py         Postgres/SQLite 스키마: raw_record · event · facility · contact · context_ts · region_stat · collect_log
  freshness.py  조회 결과의 원천·데이터 기간·수집시각·지연 여부
  species.py    축종별 질병·방역대 반경·위험지수 가중치, 질병/축종 이름 정규화
  geo.py        EPSG:5174(행안부 인허가) → WGS84 변환, 거리 계산
  collect.py    원천별 수집(mafra / data.go.kr / odcloud / 기상청 / 파일) + 정규화
  analysis.py   방역대 영향, 접촉 역추적(21일), 시공간 연쇄(Knox), 주간 추이, 위험지수
  main.py       FastAPI (/api/*) + 화면
  cli.py        init · sources · probe · collect · serve
config/sources.yaml   원천 정의 (endpoint, 필드 매핑)
web/                  화면 (브이월드 WMTS + Leaflet, Chart.js, 빌드 없음), i18n.js 한/영 문구
docker-compose.yml    전용 PostgreSQL (5433)
data/files/           수기·파일 데이터
tests/                pytest
```

## 주요 API

| 경로 | 내용 |
| --- | --- |
| `GET /api/summary?species=poultry` | 지표, 최근 위기경보, 최근 이벤트 |
| `GET /api/events?species=&kind=&from=&to=&bbox=` | 이벤트 |
| `GET /api/facilities?species=&kind=&bbox=` | 농장·시설 |
| `GET /api/events/{id}/impact` | 3/10km 방역대 내 농장·시설, 이후 21일 반경 내 이벤트 |
| `GET /api/trace?farm_key=&day=` | 발생 전 21일 진입 업체와 같은 업체가 들른 농장 |
| `GET /api/spacetime?species=` | 근접 발생 쌍, Knox 비율, 클러스터 |
| `GET /api/risk?species=` | 시군 위험지수와 요인별 기여 (데이터 없는 요인은 `missing`) |
| `GET /api/series`, `/api/weather` | 주간 추이, 기상 시계열 |
| `POST /api/events` | 이벤트 직접 입력 |
| `POST /api/collect/{id}` | 원천 수집 실행 |

## 참고

- 농식품 빅데이터 포털 36종은 호출서버 IP가 `localhost` 로 등록돼 있어 이 맥에서만 응답합니다. 서버로 옮길 때 IP 변경 신청이 필요합니다.
- 키는 `.env` 에만 둡니다(git 제외). 채팅·스크린샷으로 공유된 키는 운영 전환 때 재발급을 권합니다.
- 위험지수 가중치는 `app/species.py` 의 초기값입니다. 과거 발생 데이터가 쌓이면 회귀로 다시 추정합니다.
- 테스트: `.venv/bin/python -m pytest -q` (SQLite). Postgres로: `TEST_DATABASE_URL=postgresql+psycopg://ldt:ldt@localhost:5433/ldt_test .venv/bin/python -m pytest -q` — 테스트 DB 테이블을 지우므로 운영 DB를 지정하지 마세요.
