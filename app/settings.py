"""환경변수(.env)와 경로 설정."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _key(name: str) -> str:
    # 공공데이터포털 Encoding 키를 넣어도 httpx가 다시 인코딩하므로 한 번 디코딩해 둔다.
    return unquote(os.environ.get(name, "").strip())


DATA_GO_KR_KEY = _key("DATA_GO_KR_KEY")
MAFRA_KEY = os.environ.get("MAFRA_KEY", "").strip()
# 국토교통부 브이월드(VWorld) 오픈API 인증키 — 배경지도(WMTS) 타일에 사용
VWORLD_KEY = os.environ.get("VWORLD_KEY", "").strip()
# 서버에서 브이월드 API를 부를 때 키에 등록된 서비스 URL (주소→좌표 변환에 사용)
VWORLD_DOMAIN = os.environ.get("VWORLD_DOMAIN", "http://localhost").strip()
KAKAO_REST_KEY = os.environ.get("KAKAO_REST_KEY", "").strip()

RAW_DIR = ROOT / "data" / "raw"
FILES_DIR = ROOT / "data" / "files"
CONFIG_DIR = ROOT / "config"
WEB_DIR = ROOT / "web"

# DATABASE_URL 이 없으면 SQLite 파일로 동작(간이 실행·테스트용)
_sqlite = ROOT / os.environ.get("DB_PATH", "data/ldt.sqlite3")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip() or f"sqlite:///{_sqlite}"

# 원본 JSON 파일도 남길지 (DB raw_record 에는 항상 저장)
KEEP_RAW_FILES = os.environ.get("KEEP_RAW_FILES", "0") == "1"

for d in (_sqlite.parent, RAW_DIR, FILES_DIR):
    d.mkdir(parents=True, exist_ok=True)
