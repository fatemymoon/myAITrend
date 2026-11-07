"""수업 도입 브리핑용 후보 선정. 최근 N일 items 에서 카테고리별 상위 후보를 JSON으로 출력한다.

실행: python3 -m backend.intro_select [--topic 주제] [--days 7] [--per-category 3]

기간 기준은 published_at(원문 게시 시각)이고, 게시 시각이 없는 항목은 collected_at(수집 시각)으로 대신한다.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict

from backend import storage

# 3개 항목은 카테고리가 겹치지 않아야 하므로, 우선순위 순서로 채운다.
PREFERRED_ORDER = ["도구", "최신기술동향", "교육소식", "기술", "유행"]


def select_candidates(rows: list[dict], topic: str | None = None, per_category: int = 3) -> dict:
    """rows: storage.latest 형식의 dict 목록. 주제가 있으면 제목·매칭 키워드에 포함된 항목만 남긴다."""
    if topic:
        t = topic.lower()
        rows = [
            r for r in rows
            if t in r["title"].lower() or any(t in k.lower() for k in r["keywords_matched"])
        ]
    # 입력은 score DESC, 게시 시각 DESC 순서. 안정 정렬이라 동점은 최신 수집 순서를 유지한다.
    ordered = sorted(rows, key=lambda r: r["score"], reverse=True)

    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in ordered:
        if len(grouped[r["category"]]) < per_category:
            grouped[r["category"]].append(r)

    cats = [c for c in PREFERRED_ORDER if c in grouped] + [c for c in grouped if c not in PREFERRED_ORDER]
    return {
        "topic": topic,
        "candidates_total": len(rows),
        "categories_available": cats,
        "candidates": {c: grouped[c] for c in cats},
    }


def recent_rows(days: int) -> list[dict]:
    """최근 N일 항목 (score DESC, 게시 시각 DESC)."""
    return storage.recent_items(days)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", default=None)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--per-category", type=int, default=3)
    args = ap.parse_args()
    storage.init_db()
    result = select_candidates(recent_rows(args.days), args.topic, args.per_category)
    result["window_days"] = args.days
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
