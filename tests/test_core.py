"""핵심 로직 테스트. 네트워크를 쓰지 않는다 (소스 응답은 인라인 샘플로 대체)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend import collector, storage
from backend.config import load_profile
from backend.scoring import Scorer, detect_lang
from backend.sources import RawItem, SourceContext, parse_date, parse_feed

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Claude agent update</title><link>https://ex.com/a?utm_source=x#frag</link>
<description>&lt;p&gt;MCP tools&lt;/p&gt;</description></item>
<item><title></title><link>https://ex.com/no-title</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>GN: Claude Code plugin</title><link rel="alternate" href="https://news.example/x"/>
<summary>tool</summary></entry></feed>"""


@pytest.fixture
def profile():
    return load_profile()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """저장소를 임시 폴더(data/daily 대체)로 돌린다. 실제 data/ 는 건드리지 않는다."""
    daily = tmp_path / "daily"
    monkeypatch.setattr(storage, "DAILY_DIR", daily)
    monkeypatch.setattr(storage, "RUNS_FILE", daily / "runs.jsonl")
    storage.init_db()
    return daily


# ---- URL 정규화 / 해시
def test_canonical_url_strips_tracking_and_fragment():
    a = storage.canonical_url("HTTPS://Ex.com/a/?utm_source=x&b=1#frag")
    assert a == "https://ex.com/a?b=1"
    assert storage.url_id("https://ex.com/a") == storage.url_id("https://ex.com/a/?fbclid=1")


# ---- 파서
def test_parse_rss_keeps_valid_items_and_unescapes_summary():
    items = parse_feed(RSS, "src")
    assert len(items) == 1
    assert items[0].title == "Claude agent update"
    assert items[0].url == "https://ex.com/a?utm_source=x#frag"
    assert items[0].summary == "MCP tools"


def test_parse_atom_uses_alternate_link():
    items = parse_feed(ATOM, "gn")
    assert items[0].url == "https://news.example/x"
    assert items[0].source == "gn"


def test_parse_rejects_entity_declarations():
    evil = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY e "x">]><rss><channel><item><title>&e;</title><link>https://a</link></item></channel></rss>'
    with pytest.raises(Exception):
        parse_feed(evil, "src")


# ---- 게시일
def test_parse_date_handles_rfc822_iso_and_garbage():
    utc = timezone.utc
    assert parse_date("Fri, 06 Nov 2026 18:00:00 GMT") == datetime(2026, 11, 6, 18, tzinfo=utc)
    assert parse_date("2026-11-07T09:00:00+09:00") == datetime(2026, 11, 7, 0, tzinfo=utc)
    assert parse_date("2026-11-03T19:00:00.000Z") == datetime(2026, 11, 3, 19, tzinfo=utc)
    assert parse_date("not a date") is None
    assert parse_date("") is None and parse_date(None) is None


def test_parse_feed_reads_pubdate_and_prefers_published_over_updated():
    rss = b"""<rss><channel><item><title>t</title><link>https://a</link>
    <pubDate>Fri, 06 Nov 2026 18:00:00 GMT</pubDate></item></channel></rss>"""
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>t</title>
    <link href="https://b"/><updated>2026-11-07T00:00:00Z</updated>
    <published>2026-11-01T00:00:00Z</published></entry></feed>"""
    assert parse_feed(rss, "s")[0].published_at == datetime(2026, 11, 6, 18, tzinfo=timezone.utc)
    assert parse_feed(atom, "s")[0].published_at == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert parse_feed(RSS, "s")[0].published_at is None  # 날짜 없는 피드


# ---- 점수 / 카테고리
def test_priority_keyword_scores_higher_than_general(profile):
    s = Scorer(profile)
    hit = s.score(RawItem(title="Claude agent launches", url="https://a", source="openai"))
    assert hit is not None
    assert hit["score"] == 6.0  # Claude 3 + agent 3
    assert hit["category"] in ("도구", "최신기술동향")


def test_exclude_keyword_drops_item(profile):
    assert Scorer(profile).score(RawItem(title="Claude 광고 캠페인", url="https://a", source="x")) is None


def test_popularity_alone_does_not_pass(profile):
    raw = RawItem(title="Tesla recalls cars", url="https://a", source="geeknews", points=900)
    assert Scorer(profile).score(raw) is None


def test_popularity_boosts_matched_item_and_marks_buzz(profile):
    raw = RawItem(title="Claude Code is trending", url="https://a", source="geeknews", points=500)
    res = Scorer(profile).score(raw)
    assert res["score"] == 3 + 2  # 우선 키워드 3 + 인기 상한 2
    assert res["category"] == "유행"


def test_education_rule_wins_over_tool_rule(profile):
    raw = RawItem(title="Claude workshop for CLI users", url="https://a", source="openai")
    assert Scorer(profile).score(raw)["category"] == "교육소식"


def test_korean_title_bonus(profile):
    raw = RawItem(title="클로드 업무자동화 사례", url="https://a", source="geeknews")
    assert Scorer(profile).score(raw)["score"] == 9.5  # 우선 클로드·업무자동화·자동화 3씩 + 한글 0.5


# ---- 저장 / 중복
def test_insert_dedups_across_runs(tmp_db):
    row = {"title": "t", "url": "https://ex.com/a/", "source": "s", "category": "도구",
           "keywords_matched": ["Claude"], "score": 3.0}
    dup = dict(row, url="https://ex.com/a?utm_medium=y")
    assert storage.insert_items([row]) == 1
    assert storage.insert_items([dup]) == 0
    assert storage.stats()["total"] == 1


def test_latest_filters_by_category(tmp_db):
    base = {"source": "s", "keywords_matched": [], "score": 1.0}
    storage.insert_items([dict(base, title="a", url="https://a", category="도구"),
                          dict(base, title="b", url="https://b", category="기술")])
    got = storage.latest("도구", 10)
    assert [r["title"] for r in got] == ["a"]


def test_empty_run_still_creates_folder_and_today_file(tmp_db):
    """수집 0건이어도 data/daily 폴더와 오늘 JSONL 파일이 생겨야 한다 (CI의 git add 실패 방지)."""
    assert storage.insert_items([]) == 0
    assert tmp_db.is_dir()
    assert storage.daily_path().exists()
    assert storage.daily_path().parent == tmp_db


def test_items_append_as_jsonl_lines(tmp_db):
    storage.insert_items([{"title": "x", "url": "https://x", "source": "s", "category": "도구",
                           "keywords_matched": ["Claude"], "score": 3.0}])
    lines = storage.daily_path().read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert '"title": "x"' in lines[0]


# ---- 수집 1회: 소스 하나가 실패해도 나머지는 저장된다
def test_collector_continues_after_source_failure(tmp_db, monkeypatch):
    def bad(ctx: SourceContext):
        raise RuntimeError("HTTP 429")

    def good(ctx: SourceContext):
        return [RawItem(title="Claude MCP 가이드", url="https://ok/1", source="good")]

    monkeypatch.setattr(collector, "SOURCES", [("bad", bad), ("good", good)])
    summary = collector._run()
    assert summary["inserted"] == 1
    assert summary["failed_sources"][0]["source"] == "bad"
    assert "429" in summary["failed_sources"][0]["error"]
    last = storage.last_run()
    assert last["inserted"] == 1 and last["failed_sources"][0]["source"] == "bad"
    # 실행 기록과 오늘 파일이 모두 생겼는지
    assert storage.daily_path().exists()
    assert (tmp_db / "runs.jsonl").exists()


def test_collector_zero_items_still_writes_files(tmp_db, monkeypatch):
    """모든 소스가 0건을 돌려줘도 폴더·오늘 파일·실행 기록이 남는다."""
    monkeypatch.setattr(collector, "SOURCES", [("empty", lambda ctx: [])])
    summary = collector._run()
    assert summary["inserted"] == 0
    assert summary["per_source"]["empty"]["status"] == "성공"
    assert storage.daily_path().exists()
    assert storage.last_run() is not None


def test_format_table_lists_each_source_status(tmp_db, monkeypatch):
    def bad(ctx: SourceContext):
        raise RuntimeError("timeout")

    monkeypatch.setattr(collector, "SOURCES", [("bad", bad), ("empty", lambda ctx: [])])
    table = collector.format_table(collector._run())
    assert "bad" in table and "실패" in table
    assert "empty" in table and "성공" in table
    assert "성공 1 / 실패 1" in table


# ---- 수업 도입 후보 선정
from backend.intro_select import select_candidates  # noqa: E402


def _row(title, cat, score, kws=()):
    return {"title": title, "category": cat, "score": score, "collected_at": "2026-11-07T00:00:00+00:00",
            "keywords_matched": list(kws), "url": f"https://x/{title}", "source": "s"}


def test_intro_candidates_group_by_category_in_preferred_order():
    rows = [_row("a", "기술", 9), _row("b", "도구", 5), _row("c", "도구", 4), _row("d", "교육소식", 3)]
    res = select_candidates(rows, per_category=3)
    assert res["categories_available"] == ["도구", "교육소식", "기술"]
    assert [r["title"] for r in res["candidates"]["도구"]] == ["b", "c"]


def test_intro_topic_filters_by_title_or_keyword():
    rows = [_row("Claude 도구", "도구", 5, ["Claude"]), _row("MCP 기술", "기술", 4, ["MCP"]), _row("기타", "도구", 9)]
    res = select_candidates(rows, topic="mcp")
    assert res["candidates_total"] == 1
    assert res["candidates"]["기술"][0]["title"] == "MCP 기술"


# ---- 기간 필터 / 게시일 저장·정렬
def test_collector_drops_old_items_and_keeps_undated(tmp_db, monkeypatch):
    now = datetime.now(timezone.utc)

    def src(ctx: SourceContext):
        return [
            RawItem(title="Claude 새 글", url="https://n/1", source="s", published_at=now - timedelta(days=1)),
            RawItem(title="Claude 옛 글", url="https://n/2", source="s", published_at=now - timedelta(days=400)),
            RawItem(title="Claude 날짜 없음", url="https://n/3", source="s"),
        ]

    monkeypatch.setattr(collector, "SOURCES", [("s", src)])
    summary = collector._run()
    assert summary["per_source"]["s"]["stale"] == 1
    saved = {r["title"]: r for r in storage.all_items()}
    assert set(saved) == {"Claude 새 글", "Claude 날짜 없음"}
    assert saved["Claude 새 글"]["published_at"].startswith((now - timedelta(days=1)).date().isoformat())
    assert saved["Claude 날짜 없음"]["published_at"] is None


def test_latest_sorts_by_published_at(tmp_db):
    base = {"source": "s", "keywords_matched": [], "score": 1.0, "category": "도구"}
    storage.insert_items([
        dict(base, title="older", url="https://o", published_at="2026-11-01T00:00:00+00:00"),
        dict(base, title="newer", url="https://n", published_at="2026-11-06T00:00:00+00:00"),
    ])
    assert [r["title"] for r in storage.latest(None, 10)] == ["newer", "older"]


def _profile_korean_only(on: bool):
    def load():
        p = load_profile()
        p["collect"]["korean_only"] = on
        return p
    return load


def test_collector_korean_only_switch_drops_non_hangul(tmp_db, monkeypatch):
    """korean_only 를 켜면 한글 없는 글을 버린다 (기본 프로필은 꺼져 있음)."""
    def src(ctx: SourceContext):
        return [
            RawItem(title="Claude agent 도입기", url="https://k/1", source="s"),
            RawItem(title="Claude agent in production", url="https://k/2", source="s"),
            RawItem(title="Claude agent", url="https://k/3", source="s", summary="사내 적용 사례"),
        ]

    monkeypatch.setattr(collector, "SOURCES", [("s", src)])
    monkeypatch.setattr(collector, "load_profile", _profile_korean_only(True))
    summary = collector._run()
    assert summary["per_source"]["s"]["non_ko"] == 1
    assert {r["url"] for r in storage.all_items()} == {"https://k/1", "https://k/3"}


def test_collector_keeps_english_and_tags_lang(tmp_db, monkeypatch):
    def src(ctx: SourceContext):
        return [
            RawItem(title="Claude agent 도입기", url="https://k/1", source="s"),
            RawItem(title="Claude agent in production", url="https://k/2", source="s"),
        ]

    monkeypatch.setattr(collector, "SOURCES", [("s", src)])
    collector._run()
    langs = {r["url"]: r["lang"] for r in storage.all_items()}
    assert langs == {"https://k/1": "ko", "https://k/2": "en"}


def test_default_profile_collects_non_korean(profile):
    assert profile["collect"]["korean_only"] is False


def test_detect_lang():
    assert detect_lang("클로드 에이전트") == "ko"
    assert detect_lang("Claude agents") == "en"
    assert detect_lang("人工知能") == "other"


def test_short_english_keyword_needs_word_boundary(profile):
    s = Scorer(profile)
    # "AI" 가 Airbnb·Aim 에 걸리면 안 된다 (영어 소스에서 오탐이 생긴다)
    r = s.score(RawItem(title="Airbnb aim to hire Claude LLM", url="https://a", source="x"))
    assert "AI" not in r["keywords_matched"]
    assert s.score(RawItem(title="AI-powered tools and LLM", url="https://b", source="x")) is not None


def test_education_requires_ai_keyword(profile):
    s = Scorer(profile)
    no_ai = s.score(RawItem(title="신임 부교육감 부임", url="https://a", source="x", summary="교육 학습 강의 소식"))
    with_ai = s.score(RawItem(title="AI 교육 강의 개설", url="https://b", source="x"))
    assert no_ai is None
    assert with_ai is not None and with_ai["category"] == "교육소식"


def test_education_rule_off_when_list_empty(profile):
    profile["education_requires_ai"] = []
    s = Scorer(profile)
    r = s.score(RawItem(title="교육 강의 프롬프트 소식", url="https://a", source="x"))
    assert r is not None and r["category"] == "교육소식"


def test_korean_keywords_score_like_english(profile):
    s = Scorer(profile)
    ko = s.score(RawItem(title="클로드 에이전트 활용기", url="https://a", source="naver_d2"))
    en = s.score(RawItem(title="Claude agent 활용기", url="https://b", source="naver_d2"))
    assert ko["score"] == en["score"] == 6.5  # 우선 3 + 우선 3 + 한글 0.5
