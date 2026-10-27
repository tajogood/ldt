"""뉴스·공지 모니터링.

- 뉴스: 구글 뉴스 RSS 검색(키 없이 사용) — 축종별 검색어, NEWS_EVERY_SEC(기본 300초 = 5분)마다
- 공지: 농림축산식품부 게시판 RSS(공지·공고 / 보도자료 / 설명자료) — NOTICE_EVERY_SEC(기본 1800초)마다
  농식품부 게시물은 농업 전반이라 가축 방역 관련 단어가 있으면 '관련'으로 표시한다.
같은 기사(링크·제목)는 한 번만 저장한다. 서버가 켜져 있는 동안 별도 스레드가 돈다.
"""
from __future__ import annotations

import hashlib
import html
import os
import re
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx

from . import db

KST = timezone(timedelta(hours=9))
NEWS_EVERY = int(os.environ.get("NEWS_EVERY_SEC", "300"))
NOTICE_EVERY = int(os.environ.get("NOTICE_EVERY_SEC", "1800"))
ENABLED = os.environ.get("NEWS_MONITOR", "1") == "1"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}

# 축종별 뉴스 검색어(구글 뉴스 검색 문법: OR, 따옴표)
QUERIES = {
    "poultry": ['"조류인플루엔자"', '"고병원성 AI"', "가금 방역", '"뉴캣슬병" OR "가금티푸스"'],
    "pig": ['"아프리카돼지열병"', '"돼지열병" OR "PRRS" 돼지', "양돈 방역"],
    "cattle": ['"럼피스킨"', '"구제역"', '"브루셀라" 소 OR "결핵병" 소'],
    "all": ["가축전염병 방역", "가축 이동중지 OR 일시이동중지", "농림축산검역본부 방역"],
}
NOTICE_FEEDS = {
    "mafra_notice": ("농림축산식품부 공지·공고", "https://www.mafra.go.kr/bbs/home/791/rssList.do?row=50"),
    "mafra_press": ("농림축산식품부 보도자료", "https://www.mafra.go.kr/bbs/home/792/rssList.do?row=50"),
    "mafra_explain": ("농림축산식품부 설명자료", "https://www.mafra.go.kr/bbs/home/793/rssList.do?row=50"),
}
# 제목·요약에 있으면 '가축 방역 관련'
RELEVANT = re.compile(r"조류인플루엔자|고병원성|AI\b|가금|철새|야생조류|아프리카돼지열병|ASF|돼지열병|구제역|럼피스킨|브루셀라|결핵병|"
                      r"가축|축산|방역|살처분|이동중지|검역|전염병|양돈|양계|오리|한우|젖소|도축|사료|소독|백신")
SPECIES_WORDS = {"poultry": r"조류인플루엔자|고병원성 ?AI|가금|닭|오리|산란계|육계|철새|야생조류|뉴캣슬|가금티푸스",
                 "pig": r"돼지|양돈|ASF|PRRS|멧돼지",
                 "cattle": r"구제역|럼피스킨|한우|젖소|육우|소 |브루셀라|결핵병"}

state: dict = {"news": None, "notice": None}


def _uid(*parts: str) -> str:
    return hashlib.sha1("|".join(p or "" for p in parts).encode()).hexdigest()[:24]


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _when(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:   # 농식품부 RSS: "2026-10-27 12:22:33.846" (한국 시각)
            d = datetime.fromisoformat(s.strip()[:19]).replace(tzinfo=KST)
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _tags(text: str) -> list[str]:
    return [sp for sp, pat in SPECIES_WORDS.items() if re.search(pat, text)]


def parse_rss(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text.encode() if isinstance(xml_text, str) else xml_text)
    out = []
    for it in root.iter("item"):
        g = lambda tag: (it.findtext(tag) or "").strip()  # noqa: E731
        src = it.find("source")
        out.append({"title": _clean(g("title")), "link": g("link"), "published": _when(g("pubDate")),
                    "summary": _clean(g("description"))[:400], "publisher": (src.text or "").strip() if src is not None else ""})
    return out


def _save(items: list[dict]) -> int:
    if not items:
        return 0
    with db.session() as con:
        have = {r["uid"] for r in con.rows(
            f"SELECT uid FROM news_item WHERE uid IN ({','.join('?' * len(items))})", [i["uid"] for i in items])}
        new = [i for i in items if i["uid"] not in have]
        if new:
            db.upsert(con, "news_item", new)
    return len(new)


def fetch_news(client: httpx.Client) -> int:
    items, seen = [], set()
    for sp, qs in QUERIES.items():
        for q in qs:
            url = f"https://news.google.com/rss/search?q={quote(q + ' when:7d')}&hl=ko&gl=KR&ceid=KR:ko"
            r = client.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            for it in parse_rss(r.text):
                # 구글 뉴스 제목은 "제목 - 언론사" → 언론사 떼고, 같은 제목은 하나로
                title = re.sub(rf"\s+-\s+{re.escape(it['publisher'])}$", "", it["title"]) if it["publisher"] else it["title"]
                key = _uid("news", title)
                if key in seen:
                    continue
                seen.add(key)
                tags = sorted(set(_tags(title + " " + it["summary"]) + ([sp] if sp != "all" else [])))
                items.append({"uid": key, "kind": "news", "source": "google_news", "publisher": it["publisher"][:80],
                              "title": title[:300], "link": it["link"][:1000], "summary": "", "published": it["published"],
                              "species": ",".join(tags), "query": q[:120], "relevant": True, "fetched_at": db.now()})
            time.sleep(1)
    return _save(items)


def fetch_notices(client: httpx.Client) -> int:
    items = []
    for sid, (label, url) in NOTICE_FEEDS.items():
        r = client.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        for it in parse_rss(r.text):
            text = it["title"] + " " + it["summary"]
            items.append({"uid": _uid(sid, it["link"] or it["title"]), "kind": "notice", "source": sid, "publisher": label,
                          "title": it["title"][:300], "link": it["link"].replace("http://", "https://")[:1000],
                          "summary": it["summary"][:400], "published": it["published"],
                          "species": ",".join(_tags(text)), "query": "", "relevant": bool(RELEVANT.search(text)),
                          "fetched_at": db.now()})
    return _save(items)


def run_once(kind: str) -> dict:
    t0 = time.time()
    try:
        with httpx.Client(follow_redirects=True) as c:
            n = fetch_news(c) if kind == "news" else fetch_notices(c)
        state[kind] = {"at": db.now().isoformat(timespec="seconds"), "new": n, "ok": True, "sec": round(time.time() - t0, 1)}
    except Exception as e:
        state[kind] = {"at": db.now().isoformat(timespec="seconds"), "ok": False, "error": str(e)[:300]}
        traceback.print_exc()
    return state[kind]


def _loop() -> None:
    time.sleep(8)
    last = {"news": 0.0, "notice": 0.0}
    while True:
        now = time.time()
        if now - last["news"] >= NEWS_EVERY:
            last["news"] = now
            run_once("news")
        if now - last["notice"] >= NOTICE_EVERY:
            last["notice"] = now
            run_once("notice")
        time.sleep(15)


def start() -> None:
    if ENABLED and not state.get("thread"):
        state["thread"] = True
        threading.Thread(target=_loop, name="news-monitor", daemon=True).start()


def query(kind: str | None = None, species: str | None = None, q: str | None = None, relevant: bool = True,
          since: str | None = None, limit: int = 100, offset: int = 0) -> dict:
    w, p = ["1=1"], []
    if kind in ("news", "notice"):
        w.append("kind = ?"); p.append(kind)
    if species and species != "all":
        w.append("(species LIKE ? OR species = '' OR species IS NULL)"); p.append(f"%{species}%")   # 축종 표시 없는 일반 방역 소식도
    if q:
        w.append("(title LIKE ? OR publisher LIKE ?)"); p += [f"%{q}%", f"%{q}%"]
    if relevant:
        w.append("relevant = ?"); p.append(True)
    if since:
        w.append("fetched_at > ?"); p.append(since)
    where = " AND ".join(w)
    with db.session() as con:
        total = con.scalar(f"SELECT COUNT(*) FROM news_item WHERE {where}", p)
        items = con.rows(f"SELECT id, kind, source, publisher, title, link, summary, published, species, query, relevant, "
                         f"fetched_at FROM news_item WHERE {where} ORDER BY COALESCE(published, fetched_at) DESC, id DESC "
                         f"LIMIT ? OFFSET ?", p + [min(limit, 500), offset])
        daily = con.rows(f"SELECT SUBSTR(CAST(COALESCE(published, fetched_at) AS VARCHAR), 1, 10) AS d, kind, COUNT(*) AS n "
                         f"FROM news_item WHERE {where} GROUP BY SUBSTR(CAST(COALESCE(published, fetched_at) AS VARCHAR), 1, 10), kind "
                         f"ORDER BY 1 DESC LIMIT 60", p)
    return {"total": total, "items": items, "daily": daily,
            "status": {k: v for k, v in state.items() if k != "thread"}, "every": {"news": NEWS_EVERY, "notice": NOTICE_EVERY}}
