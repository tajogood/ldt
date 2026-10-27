#!/usr/bin/env bash
# 데이터 수집: 수기 이벤트 → 농림부 핵심 원천(group:core) → 발생 주소 좌표 변환
#   ./collect.sh            기본(core)
#   ./collect.sh extra      추가 원천까지(보험·분뇨·브루셀라 검사 35만 건 등, 오래 걸림)
set -uo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
[ -x "$PY" ] || { echo "먼저 ./run.sh 를 한 번 실행해 가상환경을 만드세요."; exit 1; }
if grep -q '^DATABASE_URL=postgresql' .env && command -v docker >/dev/null 2>&1; then
  docker compose up -d --wait ldt-db
fi
$PY -m app.cli init
$PY -m app.cli collect manual_events
$PY -m app.cli collect group:core
[ "${1:-}" = "extra" ] && $PY -m app.cli collect group:extra
$PY -m app.cli geocode --limit "${GEOCODE_LIMIT:-3000}"
$PY -m app.cli sources
