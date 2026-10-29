#!/usr/bin/env bash
# Postgres(도커) 기동 → 가상환경·패키지 → DB 테이블 생성 → 수기 이벤트 적재 → 서버 실행
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || { cp .env.example .env; echo ".env 를 만들었습니다. 키를 확인하세요."; }

if grep -q '^DATABASE_URL=postgresql' .env && command -v docker >/dev/null 2>&1; then
  docker compose up -d --wait ldt-db
fi

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

.venv/bin/python -m app.cli init
.venv/bin/python -m app.cli collect manual_events || true
.venv/bin/python -m app.cli sources
# 외부(같은 네트워크·다른 기기)에서 접속하려면 .env 에 HOST=0.0.0.0
HOST="${HOST:-$(grep -E '^HOST=' .env 2>/dev/null | cut -d= -f2)}"
.venv/bin/python -m app.cli serve --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
