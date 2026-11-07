"""SQLite 적재. 기본 경로는 data/trends.db."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from backend.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id               TEXT PRIMARY KEY,          -- sha256(정규화된 URL)
    title            TEXT NOT NULL,
    url              TEXT NOT NULL,
    source           TEXT NOT NULL,
    category         TEXT NOT NULL,
    keywords_matched TEXT NOT NULL,             -- JSON 배열
    score            REAL NOT NULL,
    collected_at     TEXT NOT NULL               -- ISO 8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_items_category ON items(category, collected_at);
CREATE INDEX IF NOT EXISTS idx_items_collected ON items(collected_at);

CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT NOT NULL,
    fetched        INTEGER NOT NULL,
    inserted       INTEGER NOT NULL,
    failed_sources TEXT NOT NULL                -- JSON 배열 {source, error}
);
"""

_TRACKING_PARAMS = {"fbclid", "gclid", "ref", "ref_src", "source"}


def canonical_url(url: str) -> str:
    """중복 판정용 정규화: 스킴/호스트 소문자, fragment·추적 파라미터 제거, 끝 슬래시 정리."""
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), "")
    )


def url_id(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = path or DB_PATH  # 호출 시점에 해석해야 테스트에서 경로를 바꿀 수 있다
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


def insert_items(rows: list[dict], path: Path | None = None) -> int:
    """URL 해시가 이미 있으면 무시(첫 수집 시각 유지). 새로 들어간 건수를 반환."""
    if not rows:
        return 0
    stamp = now_iso()
    values = [
        (
            url_id(r["url"]),
            r["title"],
            r["url"],
            r["source"],
            r["category"],
            json.dumps(r["keywords_matched"], ensure_ascii=False),
            r["score"],
            stamp,
        )
        for r in rows
    ]
    with connect(path) as conn:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO items "
            "(id, title, url, source, category, keywords_matched, score, collected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        return conn.total_changes - before


def record_run(
    started_at: str,
    fetched: int,
    inserted: int,
    failed: list[dict],
    path: Path | None = None,
) -> None:
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO runs (started_at, finished_at, fetched, inserted, failed_sources) "
            "VALUES (?, ?, ?, ?, ?)",
            (started_at, now_iso(), fetched, inserted, json.dumps(failed, ensure_ascii=False)),
        )


def latest(category: str | None, limit: int = 50, path: Path | None = None) -> list[dict]:
    sql = "SELECT * FROM items"
    args: list = []
    if category:
        sql += " WHERE category = ?"
        args.append(category)
    sql += " ORDER BY collected_at DESC, score DESC LIMIT ?"
    args.append(limit)
    with connect(path) as conn:
        return [_row(r) for r in conn.execute(sql, args)]


def stats(path: Path | None = None) -> dict:
    with connect(path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        by_cat = {
            r[0]: r[1]
            for r in conn.execute("SELECT category, COUNT(*) FROM items GROUP BY category")
        }
        by_src = {
            r[0]: r[1]
            for r in conn.execute("SELECT source, COUNT(*) FROM items GROUP BY source")
        }
        last_item = conn.execute("SELECT MAX(collected_at) FROM items").fetchone()[0]
        last_run = conn.execute(
            "SELECT started_at, finished_at, fetched, inserted, failed_sources "
            "FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return {
        "total": total,
        "by_category": by_cat,
        "by_source": by_src,
        "last_collected_at": last_item,
        "last_run": _run_row(last_run),
    }


def last_run(path: Path | None = None) -> dict | None:
    with connect(path) as conn:
        return _run_row(
            conn.execute(
                "SELECT started_at, finished_at, fetched, inserted, failed_sources "
                "FROM runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        )


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["keywords_matched"] = json.loads(d["keywords_matched"])
    return d


def _run_row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    d["failed_sources"] = json.loads(d["failed_sources"])
    return d
