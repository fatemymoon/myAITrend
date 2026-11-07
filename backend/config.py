"""경로와 프로필 로딩."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
PROFILE_PATH = ROOT_DIR / "config" / "profile.yaml"
DAILY_DIR = ROOT_DIR / "data" / "daily"  # 날짜별 수집 결과: YYYY-MM-DD.jsonl (KST 날짜)
RUNS_FILE = DAILY_DIR / "runs.jsonl"     # 실행 기록 (한 줄 = 한 번 실행)
LOG_DIR = ROOT_DIR / "logs"

# 모든 HTTP 요청에 쓰는 User-Agent. 개인 식별 정보는 넣지 않는다.
USER_AGENT = "myAITrend-collector/0.1 (personal AI trend digest; +https://github.com)"


def load_profile(path: Path = PROFILE_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    kw = data.get("keywords", {})
    data["keywords"] = {
        "priority": [str(k) for k in kw.get("priority", [])],
        "general": [str(k) for k in kw.get("general", [])],
        "exclude": [str(k) for k in kw.get("exclude", [])],
    }
    data.setdefault("categories", ["최신기술동향", "교육소식", "도구", "기술", "유행"])
    data.setdefault("category_rules", {})
    data.setdefault("scoring", {})
    data["scoring"].setdefault("min_score", 1)
    data["scoring"].setdefault("buzz_min_points", 100)
    data["scoring"].setdefault("buzz_max_bonus", 2)
    data["language"] = str((data.get("profile") or {}).get("language", "ko"))
    return data
