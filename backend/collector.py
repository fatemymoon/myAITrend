"""수집 1회 실행. 단독 실행: `python -m backend.collector`

- 소스는 순서대로 실행, 한 소스가 실패해도 나머지는 계속한다.
- profile 의 collect.korean_only 가 켜져 있으면 제목·요약에 한글이 없는 글은 버린다.
- 게시일이 profile 의 collect.max_age_days(기본 14일)보다 오래된 항목은 버린다. 게시일이 없는 항목은 판단할 수 없어 남긴다.
- URL 해시로 실행 내 중복을 먼저 제거하고, 저장 시에는 기존 일별 파일의 id와 대조해 실행 간 중복도 막는다.
- 결과는 data/daily/YYYY-MM-DD.jsonl 에 append 한다. 수집 건수가 0이어도 폴더와 오늘 파일을 만든다.
- 이 모듈은 FastAPI 스케줄러에서도 run_once()로 호출된다 (동시 실행은 락으로 막음).
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

from backend import storage
from backend.config import LOG_DIR, load_profile
from backend.net import PoliteClient
from backend.scoring import HANGUL_RE, Scorer, detect_lang
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
    storage.init_db()  # data/daily 폴더와 오늘 파일을 먼저 만든다 (0건이어도 남도록)

    korean_only = profile["collect"]["korean_only"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=profile["collect"]["max_age_days"])
    started = storage.now_iso()
    client = PoliteClient()
    ctx = SourceContext(client=client, profile=profile)

    fetched = 0
    failed: list[dict] = []
    per_source: dict[str, dict] = {}
    seen: set[str] = set()
    rows: list[dict] = []

    try:
        for name, fn in SOURCES:
            try:
                raws = fn(ctx)
            except Exception as exc:  # 소스 하나의 실패가 전체를 멈추지 않게 한다
                err = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("소스 실패 %s: %s", name, err)
                failed.append({"source": name, "error": err})
                per_source[name] = {"status": "실패", "raw": 0, "stale": 0, "non_ko": 0, "matched": 0, "error": err}
                continue

            fetched += len(raws)
            kept = 0
            stale = 0
            non_ko = 0
            for raw in raws:
                if raw.published_at is not None and raw.published_at < cutoff:
                    stale += 1
                    continue
                if korean_only and not HANGUL_RE.search(f"{raw.title} {raw.summary}"):
                    non_ko += 1
                    continue
                key = storage.url_id(raw.url)
                if key in seen:
                    continue
                seen.add(key)
                scored = scorer.score(raw)
                if scored is None:
                    continue
                rows.append({
                    "title": raw.title,
                    "url": raw.url,
                    "source": raw.source,
                    "lang": detect_lang(raw.title),
                    "published_at": raw.published_at.isoformat(timespec="seconds") if raw.published_at else None,
                    **scored,
                })
                kept += 1
            per_source[name] = {"status": "성공", "raw": len(raws), "stale": stale, "non_ko": non_ko,
                              "matched": kept, "error": None}
            log.info("소스 %s: 원본 %d건, 기간 초과 %d건, 비한국어 %d건, 관련 %d건",
                     name, len(raws), stale, non_ko, kept)
    finally:
        client.close()

    inserted = storage.insert_items(rows)
    storage.record_run(started, fetched, inserted, failed)
    summary = {
        "started_at": started,
        "daily_file": str(storage.daily_path()),
        "fetched": fetched,
        "matched": len(rows),
        "inserted": inserted,
        "sources_ok": sum(1 for v in per_source.values() if v["status"] == "성공"),
        "sources_failed": sum(1 for v in per_source.values() if v["status"] == "실패"),
        "per_source": per_source,
        "failed_sources": failed,
    }
    log.info("수집 완료: 원본 %d, 관련 %d, 신규 저장 %d, 실패 소스 %d",
             fetched, len(rows), inserted, len(failed))
    return summary


def format_table(summary: dict) -> str:
    """소스별 성공/실패 건수를 사람이 읽는 표로 만든다."""
    lines = [f"{'소스':<18}{'상태':<6}{'원본':>7}{'기간초과':>8}{'비한국어':>8}{'관련':>7}  비고"]
    for name, v in summary["per_source"].items():
        note = v["error"] or ""
        lines.append(f"{name:<18}{v['status']:<6}{v['raw']:>7}{v.get('stale', 0):>8}{v.get('non_ko', 0):>8}{v['matched']:>7}  {note}")
    lines.append(
        f"합계: 성공 {summary['sources_ok']} / 실패 {summary['sources_failed']} · "
        f"원본 {summary['fetched']}건 · 관련 {summary['matched']}건 · 신규 저장 {summary['inserted']}건"
    )
    lines.append(f"저장 파일: {summary['daily_file']}")
    return "\n".join(lines)


def main() -> int:
    setup_logging()
    summary = run_once()
    if summary.get("skipped"):
        print("건너뜀: 다른 수집이 진행 중입니다")
        return 0
    print(format_table(summary))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    # 모든 소스가 실패했을 때만 비정상 종료
    return 1 if summary["sources_failed"] == len(SOURCES) else 0


if __name__ == "__main__":
    raise SystemExit(main())
