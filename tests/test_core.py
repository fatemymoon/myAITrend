"""핵심 로직 테스트. 네트워크를 쓰지 않는다 (소스 응답은 인라인 샘플로 대체)."""

from __future__ import annotations

import pytest

from backend import collector, storage
from backend.config import load_profile
from backend.scoring import Scorer
from backend.sources import RawItem, SourceContext, parse_feed

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
    raw = RawItem(title="Tesla recalls cars", url="https://a", source="hacker_news", points=900)
    assert Scorer(profile).score(raw) is None


def test_popularity_boosts_matched_item_and_marks_buzz(profile):
    raw = RawItem(title="Claude Code is trending", url="https://a", source="hacker_news", points=500)
    res = Scorer(profile).score(raw)
    assert res["score"] == 3 + 2  # 우선 키워드 3 + 인기 상한 2
    assert res["category"] == "유행"


def test_education_rule_wins_over_tool_rule(profile):
    raw = RawItem(title="Claude workshop for CLI users", url="https://a", source="openai")
    assert Scorer(profile).score(raw)["category"] == "교육소식"


def test_korean_title_bonus(profile):
    raw = RawItem(title="클로드 업무자동화 사례", url="https://a", source="geeknews")
    assert Scorer(profile).score(raw)["score"] == 4.5  # 우선 업무자동화 3 + 일반 자동화 1 + 한글 0.5


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
        return [RawItem(title="Claude MCP guide", url="https://ok/1", source="good")]

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
