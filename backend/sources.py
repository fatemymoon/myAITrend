"""수집 소스. 소스당 함수 하나. 함수는 RawItem 목록을 반환하고, 실패하면 예외를 던진다.

각 URL은 실제 접속해 응답 형식을 확인했다 (2026-11-07 기준):
- hacker_news : hn.algolia.com/api/v1/search(_by_date) JSON, hits[].{title,url,objectID,points}
- geeknews    : news.hada.io/rss/news → feeds.feedburner.com/geeknews-feed (Atom, 20건)
- hf_daily_papers : huggingface.co/api/daily_papers JSON (50건, paper.{id,title,summary,upvotes})
- hf_blog     : huggingface.co/blog/feed.xml (RSS 2.0)
- openai      : openai.com/news/rss.xml (RSS 2.0)
- deepmind    : deepmind.google/blog/rss.xml (RSS 2.0, 100건)
- mistral     : mistral.ai/news/rss (RSS 2.0, content-type은 text/plain)
- aws_ml      : aws.amazon.com/blogs/machine-learning/feed/ (RSS 2.0)
- google_ai   : blog.google/technology/ai/rss/ → 301 → blog.google/innovation-and-ai/technology/ai/rss/ (RSS, 20건)
- github_blog : github.blog/feed/ (RSS 2.0)

제외한 소스 (확인 결과):
- arXiv API   : export.arxiv.org/robots.txt 가 "User-agent: * / Disallow: /" → robots 준수 원칙에 따라 제외
- Anthropic 블로그 : /rss.xml 404, /news 는 HTML만 제공 → RSS 소스 없음으로 제외
- ai.googleblog.com : atom.xml 404 → 제외
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    points: float | None = None  # HN 포인트 / HF upvotes 등 인기 지표


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
        title = _clean(fields.get("title"), 300)
        if not title or not link:
            continue
        summary = _clean(fields.get("summary") or fields.get("description") or fields.get("content"))
        items.append(RawItem(title=title, url=link, source=source, summary=summary))
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


# ---------------------------------------------------------------- Hacker News (Algolia)
HN_SEARCH = "https://hn.algolia.com/api/v1/search"
HN_BY_DATE = "https://hn.algolia.com/api/v1/search_by_date"


def _hn_hits_to_items(hits: list[dict]) -> list[RawItem]:
    out: list[RawItem] = []
    for h in hits:
        title = (h.get("title") or "").strip()
        if not title:
            continue
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}"
        out.append(
            RawItem(title=title, url=url, source="hacker_news", points=float(h.get("points") or 0))
        )
    return out


def fetch_hacker_news(ctx: SourceContext) -> list[RawItem]:
    """프론트페이지 + 우선 키워드 최근 3일 검색. 중복은 collector에서 URL 해시로 제거."""
    items = []
    front = ctx.client.get(HN_SEARCH, params={"tags": "front_page", "hitsPerPage": 50}).json()
    items += _hn_hits_to_items(front.get("hits", []))

    since = int((datetime.now(timezone.utc) - timedelta(days=3)).timestamp())
    for kw in ctx.profile["keywords"]["priority"]:
        params = {
            "query": kw,
            "tags": "story",
            "hitsPerPage": 20,
            "numericFilters": f"created_at_i>{since}",
        }
        try:
            res = ctx.client.get(HN_BY_DATE, params=params).json()
            items += _hn_hits_to_items(res.get("hits", []))
        except Exception as exc:  # 키워드 하나 실패는 나머지 검색을 막지 않는다
            log.warning("hacker_news 검색 실패 (%s): %s", kw, exc)
    return items


# ---------------------------------------------------------------- Hugging Face Daily Papers
def fetch_hf_daily_papers(ctx: SourceContext) -> list[RawItem]:
    data = ctx.client.get("https://huggingface.co/api/daily_papers").json()
    items = []
    for entry in data:
        paper = entry.get("paper") or {}
        pid = paper.get("id")
        title = (entry.get("title") or paper.get("title") or "").strip()
        if not pid or not title:
            continue
        items.append(
            RawItem(
                title=title,
                url=f"https://huggingface.co/papers/{pid}",
                source="hf_daily_papers",
                summary=_clean(paper.get("summary") or entry.get("summary")),
                points=float(paper.get("upvotes") or 0),
            )
        )
    return items


# ---------------------------------------------------------------- RSS/Atom 소스
fetch_geeknews = _feed_source("geeknews", "https://news.hada.io/rss/news")
fetch_hf_blog = _feed_source("hf_blog", "https://huggingface.co/blog/feed.xml")
fetch_openai = _feed_source("openai", "https://openai.com/news/rss.xml")
fetch_deepmind = _feed_source("deepmind", "https://deepmind.google/blog/rss.xml")
fetch_mistral = _feed_source("mistral", "https://mistral.ai/news/rss")
fetch_aws_ml = _feed_source("aws_ml", "https://aws.amazon.com/blogs/machine-learning/feed/")
fetch_google_ai = _feed_source("google_ai", "https://blog.google/innovation-and-ai/technology/ai/rss/")
fetch_github_blog = _feed_source("github_blog", "https://github.blog/feed/")


# 등록 순서 = 실행 순서. 새 소스는 여기에 한 줄 추가.
SOURCES: list[tuple[str, SourceFn]] = [
    ("hacker_news", fetch_hacker_news),
    ("hf_daily_papers", fetch_hf_daily_papers),
    ("geeknews", fetch_geeknews),
    ("hf_blog", fetch_hf_blog),
    ("openai", fetch_openai),
    ("deepmind", fetch_deepmind),
    ("mistral", fetch_mistral),
    ("aws_ml", fetch_aws_ml),
    ("google_ai", fetch_google_ai),
    ("github_blog", fetch_github_blog),
]
