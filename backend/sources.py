"""수집 소스. 소스당 함수 하나. 함수는 RawItem 목록을 반환하고, 실패하면 예외를 던진다.

수집 대상은 한국어 블로그·커뮤니티 게시물로 한정한다 (2026-11-07 결정).
각 URL은 실제 접속해 robots.txt 허용·응답 형식·한국어 여부를 확인했다 (2026-11-07 기준):
- geeknews    : news.hada.io/rss/news → feeds.feedburner.com/geeknews-feed (Atom, 20건)
- aws_kr      : aws.amazon.com/ko/blogs/tech/feed/ (AWS 한국 블로그, RSS 10건)
- google_kr   : developers-kr.googleblog.com/feeds/posts/default (Google 개발자 한국 블로그, Atom 15건)
- naver_d2    : d2.naver.com/d2.atom (Atom 20건)
- kakao       : tech.kakao.com/feed/ (RSS 10건)
- lycorp_ko   : techblog.lycorp.co.jp/ko/feed/index.xml (LY Corp 한국어, RSS 50건)
- banksalad   : blog.banksalad.com/rss.xml (RSS 73건)
- toss        : toss.tech/rss.xml (RSS 20건)
- daangn      : medium.com/feed/daangn (당근, RSS 10건, 일부 영어 글)
- musinsa     : medium.com/feed/musinsa-tech (RSS 10건, 일부 영어 글)

이전에 쓰다 뺀 소스:
- OpenAI, DeepMind, Mistral, AWS ML, Google AI, GitHub, HF 블로그 : 영어 블로그라 제외
- Hacker News : 뉴스 기사·저장소·논문 링크가 섞여 있어 제외
- HF Daily Papers : 논문이라 제외
- AI타임스 (aitimes.com/rss/allArticle.xml) : 수집은 되지만 블로그가 아닌 언론 기사라 제외

확인했지만 쓸 수 없는 소스:
- 우아한형제들 기술블로그, SK DEVOCEAN, Reddit, arXiv : robots.txt 가 수집기 접근을 막음
- 요즘IT (405), 컬리 (403), 쏘카 (404), 업스테이지 (404), 삼성SDS (XML 아님) : 피드 응답 없음
- NC 리서치 : 2025-01 이후 새 글 없음
- Anthropic 블로그 : RSS 없음
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

import defusedxml.ElementTree as ET

from backend.net import PoliteClient

log = logging.getLogger("collector.sources")

TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class RawItem:
    title: str
    url: str
    source: str
    summary: str = ""
    points: float | None = None  # 추천수 등 인기 지표 (현재 소스 중에는 주는 곳이 없음)
    published_at: datetime | None = None  # 원문 게시 시각 (UTC). 소스가 주지 않으면 None


@dataclass
class SourceContext:
    client: PoliteClient
    profile: dict


SourceFn = Callable[[SourceContext], list[RawItem]]


def _clean(text: str | None, limit: int = 500) -> str:
    if not text:
        return ""
    text = html.unescape(TAG_RE.sub(" ", text))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def parse_date(value: str | None) -> datetime | None:
    """RSS(RFC 822)·Atom/JSON(ISO 8601) 날짜를 UTC datetime 으로. 읽을 수 없으면 None."""
    if not value or not value.strip():
        return None
    value = value.strip()
    dt: datetime | None = None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# 게시일 필드 우선순위: 최초 게시 시각을 우선하고, 없을 때만 수정 시각(updated)을 쓴다
_DATE_FIELDS = ("pubDate", "published", "date", "issued", "updated")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(body: bytes, source: str) -> list[RawItem]:
    """RSS 2.0(<item>)와 Atom(<entry>) 공용 파서. 네임스페이스는 무시한다."""
    root = ET.fromstring(body)
    items: list[RawItem] = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        fields: dict[str, str] = {}
        dates: dict[str, str] = {}
        link = ""
        for child in node:
            name = _local(child.tag)
            if name == "link":
                rel = child.get("rel", "alternate")
                href = child.get("href")
                if href and rel == "alternate":
                    link = href
                elif not href and child.text and not link:
                    link = child.text.strip()
            elif name in ("title", "description", "summary", "content") and name not in fields:
                fields[name] = "".join(child.itertext())
            elif name in _DATE_FIELDS and name not in dates:
                dates[name] = child.text or ""
        title = _clean(fields.get("title"), 300)
        if not title or not link:
            continue
        summary = _clean(fields.get("summary") or fields.get("description") or fields.get("content"))
        published = next((d for f in _DATE_FIELDS if (d := parse_date(dates.get(f)))), None)
        items.append(RawItem(title=title, url=link, source=source, summary=summary, published_at=published))
    return items


def _feed_source(name: str, url: str) -> SourceFn:
    def fetch(ctx: SourceContext) -> list[RawItem]:
        r = ctx.client.get(url, accept="application/rss+xml, application/atom+xml, application/xml;q=0.9")
        items = parse_feed(r.body, name)
        if not items:
            raise ValueError(f"{name}: 파싱된 항목이 0건")
        return items

    fetch.__name__ = f"fetch_{name}"
    return fetch


# ---------------------------------------------------------------- RSS/Atom 소스 (한국어)
FEEDS: list[tuple[str, str]] = [
    ("geeknews", "https://news.hada.io/rss/news"),
    ("aws_kr", "https://aws.amazon.com/ko/blogs/tech/feed/"),
    ("google_kr", "https://developers-kr.googleblog.com/feeds/posts/default"),
    ("naver_d2", "https://d2.naver.com/d2.atom"),
    ("kakao", "https://tech.kakao.com/feed/"),
    ("lycorp_ko", "https://techblog.lycorp.co.jp/ko/feed/index.xml"),
    ("banksalad", "https://blog.banksalad.com/rss.xml"),
    ("toss", "https://toss.tech/rss.xml"),
    ("daangn", "https://medium.com/feed/daangn"),
    ("musinsa", "https://medium.com/feed/musinsa-tech"),
]

# 등록 순서 = 실행 순서. 새 피드는 FEEDS 에 한 줄 추가.
SOURCES: list[tuple[str, SourceFn]] = [(name, _feed_source(name, url)) for name, url in FEEDS]
