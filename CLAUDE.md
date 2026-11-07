# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -r requirements.txt -r requirements-dev.txt   # runtime + pytest
python -m pytest -q                                       # all tests (26, no network)
python -m pytest tests/test_core.py::test_collector_keeps_only_korean_posts -q   # single test
python -m backend.collector                               # one collection run (real network)
uvicorn backend.main:app --port 8000                      # API + homepage + in-process 10-min scheduler
python -m backend.intro_select                            # intro candidate selection (CLI)
```

There is no linter or build step configured. Run only one uvicorn worker: the scheduler is in-process and the collector lock (`_run_lock`) is per-process.

## Architecture

**Data flow:** `config/profile.yaml` → `backend/sources.py` (fetch + parse per source) → `backend/net.py` (polite HTTP) → `backend/scoring.py` (category/score) → `backend/collector.py` (filter, dedupe, summarize) → `backend/storage.py` (JSONL append) → `backend/main.py` / `intro_select.py` (read back).

- **Storage is JSONL, not a database.** Items go to `data/daily/YYYY-MM-DD.jsonl` (KST date, append-only). Run records go to `data/daily/runs.jsonl`. `storage.all_items()` reads every daily file, so the dedupe key `id` (SHA-256 of the canonicalized URL, see `canonical_url`) is checked across all days. `storage.init_db()` is a historical name that only creates the folder and today's file.
- **Sources are plain functions** registered in the `SOURCES` list in `sources.py`. Each returns `RawItem`s or raises. `collector._run` catches per-source exceptions, so one failing source never stops the run; its failure is recorded in `runs.jsonl`. Adding a source means writing a function and appending it to `SOURCES`.
- **Filtering happens in `collector._run`, in this order:** drop items older than `collect.max_age_days` (undated items are kept), drop non-Hangul items when `collect.korean_only` is set, dedupe by URL within the run, score via `Scorer` (items that score `None` are dropped), then `storage.insert_items` dedupes against existing files.
- **Scoring is keyword-driven from the profile.** Priority keywords weigh 3, general keywords 1, excluded keywords drop the item. Category comes from `category_rules` in `profile.yaml` (first category whose keyword list matches wins), then the 유행 rule, then the default 최신기술동향. Popularity alone never passes: an item needs at least one profile keyword, and points only add a bonus.
- **Network politeness lives in `PoliteClient`:** a fixed User-Agent, per-host minimum interval, robots.txt checks (401/403 on robots.txt means disallow-all; unreachable robots.txt also disallows for that run), and a 10 MB response cap. All source fetches must go through it.
- **Two entry points run collection:** the CLI (`python -m backend.collector`, used by GitHub Actions) and the FastAPI lifespan scheduler in `main.py`. Both call `collector.run_once()`.
- **Frontend** is a single static file, `frontend/index.html`, served at `/` and reading `/latest` and `/stats`.

## Automation

`.github/workflows/collect.yml` runs the collector every 30 minutes (and on manual dispatch) and commits only changes under `data/` with the `github-actions[bot]` identity. Pushes use `git pull --rebase` first. `concurrency` serializes runs without cancelling queued ones. Because the workflow commits to the branch it runs on, avoid force-pushing `main` while collection is active.

`.claude/agents/` and `.claude/commands/` define a Korean-language operations pipeline (heal → ops-monitor → deploy-guardian; log, intro). Commit prefixes used in this repo are `fix:`, `feat:`, `log:`, `ops/`-style branches.

## Known inconsistencies

- `README.md` and the workflow say 30-minute collection; `backend/main.py` schedules the in-process job every 10 minutes (`SCHEDULE_MINUTES`). Treat `main.py` as the source of truth for the server, the workflow for GitHub Actions.
- `.claude/commands/heal.md` and `.claude/agents/ops-monitor.md` still refer to `data/trends.db` and a 10-minute cycle. Storage is now `data/daily/*.jsonl`; update those files before relying on their checks.
- `data/daily/` is committed by automation. Do not hand-edit those JSONL files; they are append-only and deduped by `id`.

## Language

Source comments, docstrings, log messages, and the profile are in Korean; keep new comments and user-facing strings consistent with that.
