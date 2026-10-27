#!/usr/bin/env bash
# 축산차량 방문(KAHIS) 수집기 — 웹 서버(./run.sh)와 따로, 별도 터미널에서 실행
#   ./visits.sh              계속 실행(최근 3년 채우기 → 매일 갱신)
#   ./visits.sh --status     현황    ./visits.sh --probe   접속 확인    ./visits.sh --help
set -euo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
[ -x "$PY" ] || { echo "먼저 ./run.sh 를 한 번 실행해 가상환경을 만드세요."; exit 1; }
if grep -q '^DATABASE_URL=postgresql' .env && command -v docker >/dev/null 2>&1; then
  docker compose up -d --wait ldt-db >/dev/null
fi
mkdir -p data/logs
# 화면에도 보이고 data/logs/visits.log 에도 남김. 맥 잠자기 방지(caffeinate)가 있으면 켜 둔 채로 실행
if command -v caffeinate >/dev/null 2>&1; then
  exec caffeinate -i "$PY" collect_visits.py "$@" 2>&1 | tee -a data/logs/visits.log
else
  exec "$PY" collect_visits.py "$@" 2>&1 | tee -a data/logs/visits.log
fi
