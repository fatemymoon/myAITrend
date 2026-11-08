"""수집 소스. 소스당 함수 하나. 함수는 RawItem 목록을 반환하고, 실패하면 예외를 던진다.

수집 대상은 처음엔 한국어 블로그·커뮤니티로 한정했으나(2026-11-07), 2026-11-08 에 영어 소스와 언론 매체까지 넓혔다.
영어 등 비한국어 항목은 그대로 저장하고 lang 필드(en/other)로 표시한다. 한글 번역은 수집이 아니라 요약 단계에서 한다.
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
- edupress    : edupress.kr/rss/allArticle.xml (에듀프레스, 교육 정책 매체, RSS 50건, robots 허용)
- kyosu       : kyosu.net/rss/allArticle.xml (교수신문, 대학·AI 관련 기사 비중 높음, RSS 50건, robots 허용)
- newsedu     : newsedu.kr/rss/allArticle.xml (에듀뉴스, 시도교육청 AI·연수 소식, RSS 50건, robots 허용)
  위 3곳은 언론 매체다. 교육 분야 기사가 블로그에 거의 없어 예외로 넣었다. AI·교육 키워드가 없는 정책·인사 기사는 점수 필터가 걸러낸다.

영어 소스 (2026-11-08 추가, 모두 robots 허용·응답 확인): openai, deepmind, huggingface, github_ai, aws_ml, mistral,
mit_tr, techcrunch_ai, verge_ai, wired_ai, simonwillison, latent_space, oneusefulthing, jackclark, interconnects,
producthunt_ai (AI 도구 신작), thejournal·edtechmagazine (미국 교육 기술), hn_ai (Hacker News AI 검색)
한국어 AI·IT 매체 (같은 날 추가): aitimes, itworld_kr, bloter

이전에 쓰다 뺀 소스:
- HF Daily Papers : 논문이라 제외

확인했지만 쓸 수 없는 소스 (2026-11-08 영어 후보 포함):
- Import AI(substack)·The Batch(deeplearning.ai)·Class Central·CIO Korea : robots 또는 응답 403
- VentureBeat (429), Anthropic 뉴스 (404), ai-school tistory (406), ArsTechnica (robots 405: 정책 확인 불가라 제외)
- Inside Higher Ed (피드에 정의되지 않은 XML 엔티티라 파서가 거부), eSchool News (XML 선언 앞 공백)
- Google AI·Khan Academy·EdSurge : 응답은 되지만 프로필 키워드 통과 0건
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
    # 교육 전문 매체 (2026-11-08 추가): 교육소식 카테고리를 채우기 위한 예외. 블로그 한정 원칙에서 벗어난다.
    ("edupress", "https://www.edupress.kr/rss/allArticle.xml"),
    ("kyosu", "https://www.kyosu.net/rss/allArticle.xml"),
    ("newsedu", "https://www.newsedu.kr/rss/allArticle.xml"),
    # 한국어 AI·IT 매체
    ("aitimes", "https://www.aitimes.com/rss/allArticle.xml"),
    ("itworld_kr", "https://www.itworld.co.kr/rss/index.php"),
    ("bloter", "https://www.bloter.net/rss/allArticle.xml"),
    # 영어 소스 (lang=en 으로 저장, 요약 단계에서 한글 번역)
    ("openai", "https://openai.com/news/rss.xml"),
    ("deepmind", "https://deepmind.google/blog/rss.xml"),
    ("huggingface", "https://huggingface.co/blog/feed.xml"),
    ("github_ai", "https://github.blog/ai-and-ml/feed/"),
    ("aws_ml", "https://aws.amazon.com/blogs/machine-learning/feed/"),
    ("mistral", "https://mistral.ai/rss.xml"),
    ("mit_tr", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
    ("techcrunch_ai", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("verge_ai", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("wired_ai", "https://www.wired.com/feed/tag/ai/latest/rss"),
    ("simonwillison", "https://simonwillison.net/atom/everything/"),
    ("latent_space", "https://www.latent.space/feed"),
    ("oneusefulthing", "https://www.oneusefulthing.org/feed"),
    ("jackclark", "https://jack-clark.net/feed/"),
    ("interconnects", "https://www.interconnects.ai/feed"),
    ("producthunt_ai", "https://www.producthunt.com/feed?category=artificial-intelligence"),
    ("thejournal", "https://www.thejournal.com/rss-feeds/all-articles.aspx"),
    ("edtechmagazine", "https://www.edtechmagazine.com/k12/rss.xml"),
    ("hn_ai", "https://hnrss.org/frontpage?q=AI+OR+LLM+OR+Claude"),
]

# 등록 순서 = 실행 순서. 새 피드는 FEEDS 에 한 줄 추가.
SOURCES: list[tuple[str, SourceFn]] = [(name, _feed_source(name, url)) for name, url in FEEDS]
