"""프로필 키워드 기반 규칙으로 카테고리와 관련도 점수를 계산한다 (LLM 호출 없음)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from backend.sources import RawItem

HANGUL_RE = re.compile(r"[가-힣]")
LATIN_RE = re.compile(r"[A-Za-z]")
DEFAULT_CATEGORY = "최신기술동향"
BUZZ_CATEGORY = "유행"
EDU_CATEGORY = "교육소식"


def detect_lang(title: str) -> str:
    """제목 기준 언어 태그: 한글이 있으면 ko, 영문자가 있으면 en, 그 외 other.
    번역은 수집 때 하지 않는다. 요약을 쓰는 쪽(루틴·log-writer·intro-writer)이 en/other 를 한글로 옮긴다."""
    if HANGUL_RE.search(title):
        return "ko"
    return "en" if LATIN_RE.search(title) else "other"


def _pattern(keyword: str) -> re.Pattern[str]:
    # 앞쪽 글자 경계를 본다: "agent"는 "agents"에 매칭, "AI"는 "email"에 매칭되지 않음
    # 3글자 이하 영문 키워드(AI, MCP, cli ...)는 뒤쪽 경계도 본다: "AI"가 "Airbnb"·"Aim"에, "cli"가 "client"에 걸리지 않게
    # (복수형 s 는 허용: "AIs"). 영어 소스를 받으면서 필요해졌다.
    head = r"(?<![A-Za-z0-9])" + re.escape(keyword)
    if keyword.isascii() and len(keyword) <= 3:
        head += r"(?:s)?(?![A-Za-z0-9])"
    return re.compile(head, re.IGNORECASE)


@dataclass
class Scorer:
    profile: dict
    _priority: list[tuple[str, re.Pattern]] = field(init=False)
    _general: list[tuple[str, re.Pattern]] = field(init=False)
    _exclude: list[re.Pattern] = field(init=False)
    _rules: list[tuple[str, list[re.Pattern]]] = field(init=False)
    _buzz_sources: set[str] = field(init=False)
    _edu_ai: list[re.Pattern] = field(init=False)

    def __post_init__(self) -> None:
        kw = self.profile["keywords"]
        self._priority = [(k, _pattern(k)) for k in kw["priority"]]
        self._general = [(k, _pattern(k)) for k in kw["general"]]
        self._exclude = [_pattern(k) for k in kw["exclude"]]

        rules_raw = dict(self.profile.get("category_rules", {}))
        self._buzz_sources = set(rules_raw.pop("유행_sources", []))
        self._rules = [
            (cat, [_pattern(k) for k in words])
            for cat, words in rules_raw.items()
            if cat in self.profile["categories"]
        ]
        # 교육소식은 AI 키워드가 하나라도 있어야 한다. 목록이 비어 있으면 규칙을 쓰지 않는다.
        self._edu_ai = [_pattern(k) for k in self.profile.get("education_requires_ai", [])]
        self._korean = self.profile.get("language") == "ko"
        s = self.profile["scoring"]
        self._min_score = float(s["min_score"])
        self._buzz_min = float(s["buzz_min_points"])
        self._buzz_max = float(s["buzz_max_bonus"])

    def score(self, raw: RawItem) -> dict | None:
        """관련도가 기준 미만이거나 제외 키워드가 걸리면 None."""
        text = f"{raw.title} {raw.summary}"
        if any(p.search(text) for p in self._exclude):
            return None

        prio = [k for k, p in self._priority if p.search(text)]
        gen = [k for k, p in self._general if p.search(text)]
        matched = list(dict.fromkeys(prio + gen))
        if not matched:  # 프로필 키워드가 하나도 없으면 인기만으로는 넣지 않는다
            return None

        base = 3.0 * len(prio) + 1.0 * len(gen)
        buzz = 0.0
        if raw.points and raw.points >= self._buzz_min:
            buzz = min(self._buzz_max, raw.points / self._buzz_min)
        korean = 0.5 if self._korean and HANGUL_RE.search(raw.title) else 0.0

        total = round(base + buzz + korean, 2)
        if total < self._min_score:
            return None

        category = self._category(text, raw)
        if category == EDU_CATEGORY and self._edu_ai and not any(p.search(text) for p in self._edu_ai):
            return None  # AI와 무관한 교육 기사(인사·정책 등)는 수집하지 않는다

        return {
            "category": category,
            "keywords_matched": matched,
            "score": total,
        }

    def _category(self, text: str, raw: RawItem) -> str:
        for cat, patterns in self._rules:
            if any(p.search(text) for p in patterns):
                return cat
        if raw.source in self._buzz_sources and raw.points and raw.points >= self._buzz_min:
            return BUZZ_CATEGORY
        return DEFAULT_CATEGORY
