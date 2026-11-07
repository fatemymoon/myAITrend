---
description: 오늘(KST) 수집분을 요약해 logs/YYYY-MM-DD.md 를 작성하고 deploy-guardian 으로 커밋·푸시 (log-writer → deploy-guardian)
argument-hint: "[추가 메모, 선택]"
---

너는 오케스트레이터다. 아래 두 단계를 **이 순서대로** Agent 도구로 실행한다.

추가 메모가 있으면 `$ARGUMENTS` 로 받는다. 있으면 log-writer 에게 넘겨 "## 메모 (수동)" 섹션에 그대로 넣게 한다.

## 단계 1 — log-writer (로그 작성)
- Agent 도구, `subagent_type: "log-writer"`.
- 브리프: "오늘(KST) 수집분을 집계해 logs/YYYY-MM-DD.md 를 지정 템플릿대로 작성하라. 추측은 '추정:'으로 표시하고, 커밋하지 마라. 작성한 파일 경로를 보고하라." 메모가 있으면 `$ARGUMENTS` 를 덧붙인다.
- 중단 조건: 파일이 만들어지지 않았거나 보고가 실패이면 **중단**한다. 단계 2를 실행하지 않는다.
- 성공하면 작성된 파일 경로(예: `logs/2026-11-07.md`)를 기록해 단계 2에 넘긴다.

## 단계 2 — deploy-guardian (커밋·푸시)
- Agent 도구, `subagent_type: "deploy-guardian"`.
- 브리프: "아래 파일 하나만 테스트 게이트 후 커밋하고 푸시하라. 커밋 종류: log. 요약: <YYYY-MM-DD 일일 운영 로그>. 파일: <단계 1의 경로>. 금지 파일 검사를 반드시 수행하라. logs/ 의 다른 파일은 스테이징하지 마라."
- 중단 조건: 상태가 "중단"이면 그 사유를 그대로 보고한다. "커밋할 변경 없음"이면 같은 날짜 로그가 이미 최신이라는 뜻이므로 그대로 보고한다.

## 최종 보고
```
## /log 결과
- 단계 1 log-writer: <생성 파일 경로, 수집 건수 한 줄>
- 단계 2 deploy-guardian: <상태 한 줄 / 실행 안 함>
- 중단 지점: <없음 / 단계 N: 사유>
```
