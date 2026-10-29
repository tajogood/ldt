"""명령줄 도구.

python -m app.cli init                  # DB 생성
python -m app.cli sources               # 원천 목록과 설정 상태
python -m app.cli probe <id> [n]        # 원본 필드·샘플 확인 (DB에 넣지 않음)
python -m app.cli collect <id|all|group:core|group:extra> [--limit N]
python -m app.cli geocode [--limit N]   # 발생 주소 → 좌표 (브이월드)
python -m app.cli serve [--port 8000]
"""
from __future__ import annotations

import argparse
import json
import sys

from . import collect, db


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("sources")
    p = sub.add_parser("probe")
    p.add_argument("id")
    p.add_argument("n", nargs="?", type=int, default=3)
    c = sub.add_parser("collect")
    c.add_argument("id")
    c.add_argument("--limit", type=int)
    g = sub.add_parser("geocode")
    g.add_argument("--limit", type=int, default=2000)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        # 60초 안에 켜지지 않으면 어디서 멈췄는지(모든 스레드의 호출 위치)를 로그에 남긴다 — 켜지면 앱이 취소함
        import faulthandler
        faulthandler.dump_traceback_later(60, repeat=False)
        print("DB 확인 중…", flush=True)
    db.init()
    if a.cmd == "init":
        print("DB 준비 완료")
    elif a.cmd == "sources":
        for src in collect.load_sources():
            mark = "●" if collect.is_configured(src) else "○"
            print(f" {mark} {src['id']:30s} {src.get('group', '-'):6s} {src['provider']:9s} → {src['target']:12s} {src['title']}")
        print("\n● 설정됨  ○ endpoint/service_id/키/파일 필요")
    elif a.cmd == "probe":
        out = collect.probe(a.id, a.n)
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    elif a.cmd == "collect":
        srcs = collect.load_sources()
        if a.id == "all":
            ids = [s["id"] for s in srcs if collect.is_configured(s)]
        elif a.id.startswith("group:"):
            grp = a.id.split(":", 1)[1]
            ids = [s["id"] for s in srcs if s.get("group") == grp and collect.is_configured(s)]
        else:
            ids = [a.id]
        failed = 0
        for sid in ids:
            try:
                n = collect.run(sid, a.limit)
                print(f"  ✓ {sid}: {n}행")
            except Exception as e:
                failed += 1
                print(f"  ✗ {sid}: {e}")
        return 1 if failed else 0
    elif a.cmd == "geocode":
        from . import geocode

        print(geocode.run(a.limit))
    elif a.cmd == "serve":
        import uvicorn

        print(f"http://{a.host}:{a.port} 에서 실행 — 앱을 불러오는 중…", flush=True)
        uvicorn.run("app.main:app", host=a.host, port=a.port, reload=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
