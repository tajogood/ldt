"""테스트 DB 준비.

기본은 임시 SQLite. Postgres로 돌리려면:
  TEST_DATABASE_URL=postgresql+psycopg://ldt:ldt@localhost:5433/ldt_test pytest -q
(해당 DB의 테이블을 지우고 다시 만든다 — 운영 DB를 지정하지 마세요.)
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["AUTO_COLLECT"] = "0"   # 테스트 중에는 자동 수집 스레드를 띄우지 않음
os.environ["NEWS_MONITOR"] = "0"
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{Path(_tmp) / 'test.sqlite3'}"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402

db.md.drop_all(db.engine)
db.init()
