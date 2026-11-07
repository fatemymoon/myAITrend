"""FastAPI 서버. 실행: `uvicorn backend.main:app --reload`

- /health : 상태 확인
- /latest?category=&limit= : 최신 수집 항목
- /stats  : 카테고리/소스별 건수, 마지막 실행 결과
- 시작 시 BackgroundScheduler가 10분 주기로 수집기를 돌린다 (워커는 1개로 실행할 것).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Query

from backend import storage
from backend.collector import run_once, setup_logging
from backend.config import load_profile

SCHEDULE_MINUTES = 10
scheduler = BackgroundScheduler(
    job_defaults={"max_instances": 1, "coalesce": True},
    timezone="UTC",
)


def _scheduled_job() -> None:
    run_once()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    storage.init_db()
    scheduler.add_job(
        _scheduled_job,
        "interval",
        minutes=SCHEDULE_MINUTES,
        id="collect",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=1),
        replace_existing=True,
    )
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="myAITrend", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    try:
        storage.init_db()
        db_ok = True
    except Exception:
        db_ok = False
    job = scheduler.get_job("collect")
    return {
        "status": "ok" if db_ok else "degraded",
        "db_ok": db_ok,
        "scheduler_running": scheduler.running,
        "next_run_at": job.next_run_time.isoformat() if job and job.next_run_time else None,
        "last_run": storage.last_run(),
    }


@app.get("/latest")
def latest(
    category: str | None = Query(None, description="카테고리 이름 (config/profile.yaml 의 categories)"),
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    allowed = load_profile()["categories"]
    if category is not None and category not in allowed:
        raise HTTPException(status_code=400, detail={"error": "unknown category", "allowed": allowed})
    items = storage.latest(category, limit)
    return {"category": category, "count": len(items), "items": items}


@app.get("/stats")
def stats() -> dict:
    return storage.stats()
