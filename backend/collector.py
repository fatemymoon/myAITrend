"""수집 1회 실행. 단독 실행: `python -m backend.collector`

- 소스는 순서대로 실행, 한 소스가 실패해도 나머지는 계속한다.
- URL 해시로 실행 내 중복을 먼저 제거하고, DB에는 INSERT OR IGNORE로 실행 간 중복도 막는다.
- 이 모듈은 FastAPI 스케줄러에서도 run_once()로 호출된다 (동시 실행은 락으로 막음).
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler

from backend import storage
from backend.config import LOG_DIR, load_profile
from backend.net import PoliteClient
from backend.scoring import Scorer
from backend.sources import SOURCES, SourceContext

log = logging.getLogger("collector")
_run_lock = threading.Lock()


def setup_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 요청마다 찍히는 INFO 로그 억제
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(LOG_DIR / "collector.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def run_once() -> dict:
    if not _run_lock.acquire(blocking=False):
        log.info("이미 수집이 진행 중이라 이번 실행은 건너뜁니다")
        return {"skipped": True}
    try:
        return _run()
    finally:
        _run_lock.release()


def _run() -> dict:
    profile = load_profile()
    scorer = Scorer(profile)
    storage.init_db()

    started = storage.now_iso()
    client = PoliteClient()
    ctx = SourceContext(client=client, profile=profile)

    fetched = 0
    failed: list[dict] = []
    per_source: dict[str, int] = {}
    seen: set[str] = set()
    rows: list[dict] = []

    try:
        for name, fn in SOURCES:
            try:
                raws = fn(ctx)
            except Exception as exc:  # 소스 하나의 실패가 전체를 멈추지 않게 한다
                log.warning("소스 실패 %s: %s: %s", name, type(exc).__name__, exc)
                failed.append({"source": name, "error": f"{type(exc).__name__}: {exc}"[:300]})
                continue

            fetched += len(raws)
            kept = 0
            for raw in raws:
                key = storage.url_id(raw.url)
                if key in seen:
                    continue
                seen.add(key)
                scored = scorer.score(raw)
                if scored is None:
                    continue
                rows.append({"title": raw.title, "url": raw.url, "source": raw.source, **scored})
                kept += 1
            per_source[name] = kept
            log.info("소스 %s: 원본 %d건, 관련 %d건", name, len(raws), kept)
    finally:
        client.close()

    inserted = storage.insert_items(rows)
    storage.record_run(started, fetched, inserted, failed)
    summary = {
        "started_at": started,
        "fetched": fetched,
        "matched": len(rows),
        "inserted": inserted,
        "per_source_matched": per_source,
        "failed_sources": failed,
    }
    log.info("수집 완료: 원본 %d, 관련 %d, 신규 저장 %d, 실패 소스 %d",
             fetched, len(rows), inserted, len(failed))
    return summary


def main() -> int:
    setup_logging()
    summary = run_once()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary.get("skipped"):
        return 0
    # 모든 소스가 실패했을 때만 비정상 종료
    return 1 if len(summary["failed_sources"]) == len(SOURCES) else 0


if __name__ == "__main__":
    raise SystemExit(main())
