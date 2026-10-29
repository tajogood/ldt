#!/usr/bin/env bash
# 웹서버만 다시 켠다(차량방문 수집기 ./visits.sh 는 건드리지 않음).
#   ./restart.sh          → git pull 없이 재시작
#   ./restart.sh pull     → 새 코드 받고 재시작
# 외부에서 접속하려면 .env 에 HOST=0.0.0.0 (기본 127.0.0.1)
set -uo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-$(grep -E '^PORT=' .env 2>/dev/null | cut -d= -f2)}"; PORT="${PORT:-8000}"

if [ "${1:-}" = "pull" ]; then
  git pull --ff-only || { echo "git pull 실패 — 직접 고친 파일이 있으면 'git status' 로 확인하세요"; exit 1; }
fi

# 1) 지금 떠 있는 웹서버 끄기: run.sh 와 그 포트를 쓰는 프로세스
pkill -f "app.cli serve" 2>/dev/null
pkill -f "bash ./run.sh" 2>/dev/null
for i in $(seq 1 20); do
  lsof -ti tcp:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 || break
  [ "$i" = 10 ] && lsof -ti tcp:"$PORT" -sTCP:LISTEN | xargs kill -9 2>/dev/null
  sleep 0.5
done
if lsof -ti tcp:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "포트 $PORT 를 쓰는 프로세스가 아직 있습니다: $(lsof -ti tcp:$PORT -sTCP:LISTEN | tr '\n' ' ')"; exit 1
fi

# 2) 백그라운드로 다시 켜기(터미널을 닫아도 계속), 로그는 data/logs/server.log
mkdir -p data/logs
nohup ./run.sh > data/logs/server.log 2>&1 &
echo "켜는 중… (로그: data/logs/server.log)"
for i in $(seq 1 180); do
  if curl -s "http://127.0.0.1:$PORT/api/version" >/dev/null 2>&1; then
    echo "실행 중: $(curl -s http://127.0.0.1:$PORT/api/version)"; exit 0
  fi
  if ! pgrep -f "bash ./run.sh" >/dev/null 2>&1; then
    echo "run.sh 가 멈췄습니다. 로그 끝부분:"; tail -25 data/logs/server.log; exit 1
  fi
  [ $((i % 15)) = 0 ] && echo "  …아직 준비 중(${i}초) · 마지막 로그: $(tail -1 data/logs/server.log | cut -c1-100)"
  sleep 1
done
echo "3분 안에 뜨지 않았습니다. 로그 끝부분:"; tail -25 data/logs/server.log
echo "다른 서버·수집기가 DB를 잡고 있을 수 있습니다:  ps -ef | grep -E 'app.cli|collect_visits|run.sh' | grep -v grep"
exit 1
