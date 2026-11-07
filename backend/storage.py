"""JSONL 적재. 저장 위치: data/daily/YYYY-MM-DD.jsonl (KST 날짜별, append 전용).

- 항목 한 줄 = 수집 항목 하나 (id, title, url, source, category, keywords_matched, score, published_at, collected_at)
  published_at 은 원문 게시 시각(UTC, 없으면 null), collected_at 은 수집 시각
- 정렬·기간 필터는 게시 시각 기준이고, 게시 시각이 없으면 수집 시각으로 대신한다
- 실행 기록은 data/daily/runs.jsonl 에 한 줄씩 append
- 폴더와 오늘 파일은 수집 건수가 0이어도 항상 만든다 (touch_today)
- 중복 판정은 전체 일별 파일의 id 집합으로 한다
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from backend.config import DAILY_DIR, RUNS_FILE

KST = ZoneInfo("Asia/Seoul")
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
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def url_id(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_kst() -> date:
    return datetime.now(KST).date()


def daily_path(day: date | None = None) -> Path:
    return DAILY_DIR / f"{(day or today_kst()).isoformat()}.jsonl"


def init_db() -> None:
    """이름은 호환을 위해 init_db 로 두었다. 폴더와 오늘 파일을 만든다."""
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    touch_today()


def touch_today() -> Path:
    path = daily_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    return path


def _item_files() -> list[Path]:
    if not DAILY_DIR.exists():
        return []
    return sorted(p for p in DAILY_DIR.glob("*.jsonl") if p.name != RUNS_FILE.name)


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def all_items() -> list[dict]:
    items: list[dict] = []
    for f in _item_files():
        items.extend(_read_jsonl(f))
    return items


def insert_items(rows: list[dict]) -> int:
    """id가 이미 있으면 건너뛴다(첫 수집 시각 유지). 새로 쓴 건수를 반환. 0건이어도 오늘 파일은 만든다."""
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    path = touch_today()
    seen = {r["id"] for r in all_items()}
    stamp = now_iso()
    new_lines = []
    for r in rows:
        uid = url_id(r["url"])
        if uid in seen:
            continue
        seen.add(uid)
        new_lines.append(
            json.dumps(
                {
                    "id": uid,
                    "title": r["title"],
                    "url": r["url"],
                    "source": r["source"],
                    "category": r["category"],
                    "keywords_matched": r["keywords_matched"],
                    "score": r["score"],
                    "published_at": r.get("published_at"),
                    "collected_at": stamp,
                },
                ensure_ascii=False,
            )
        )
    if new_lines:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(new_lines) + "\n")
    return len(new_lines)


def record_run(started_at: str, fetched: int, inserted: int, failed: list[dict]) -> None:
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {
            "started_at": started_at,
            "finished_at": now_iso(),
            "fetched": fetched,
            "inserted": inserted,
            "failed_sources": failed,
        },
        ensure_ascii=False,
    )
    with RUNS_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def runs() -> list[dict]:
    if not RUNS_FILE.exists():
        return []
    return _read_jsonl(RUNS_FILE)


def last_run() -> dict | None:
    rs = runs()
    return rs[-1] if rs else None


def item_time(r: dict) -> str:
    """정렬·기간 비교용 시각: 게시 시각, 없으면 수집 시각 (둘 다 UTC ISO 문자열)."""
    return r.get("published_at") or r["collected_at"]


def latest(category: str | None, limit: int = 50) -> list[dict]:
    items = all_items()
    if category:
        items = [r for r in items if r["category"] == category]
    items.sort(key=lambda r: (item_time(r), r["score"]), reverse=True)
    return items[:limit]


def recent_items(days: int) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    items = [r for r in all_items() if item_time(r) >= since]
    items.sort(key=lambda r: (r["score"], item_time(r)), reverse=True)
    return items


def stats() -> dict:
    items = all_items()
    by_cat: dict[str, int] = {}
    by_src: dict[str, int] = {}
    for r in items:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
    return {
        "total": len(items),
        "by_category": by_cat,
        "by_source": by_src,
        "last_collected_at": max((r["collected_at"] for r in items), default=None),
        "last_run": last_run(),
    }
