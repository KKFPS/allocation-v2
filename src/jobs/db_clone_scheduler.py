"""In-process hourly scheduler for source → destination DB cloning."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import psycopg2
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config import (
    CLONE_CLIENT_ID,
    CLONE_DB_SCHEDULER_ENABLED,
    CLONE_SITE_IDS,
)
from src.jobs.db_clone import (
    CloneStats,
    DbConfig,
    parse_site_ids,
    previous_utc_hour_window,
    run_clone,
)
from src.utils.logging_config import logger

# Stable advisory-lock key for cross-worker / cross-instance mutual exclusion.
DB_CLONE_ADVISORY_LOCK_KEY = 0x434C4F4E  # 'CLON'


class DbCloneInProgressError(Exception):
    """Raised when another clone job already holds the advisory lock."""


def _configured_site_ids() -> List[int]:
    if not CLONE_SITE_IDS:
        return []
    return parse_site_ids(CLONE_SITE_IDS) or []


def _try_advisory_lock() -> Optional[psycopg2.extensions.connection]:
    config = DbConfig.from_env("DEST_")
    conn = psycopg2.connect(
        user=config.user,
        password=config.password,
        database=config.database,
        host=config.host,
        port=config.port,
    )
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (DB_CLONE_ADVISORY_LOCK_KEY,))
        acquired = cur.fetchone()[0]
    if not acquired:
        conn.close()
        return None
    return conn


def _release_advisory_lock(conn: psycopg2.extensions.connection) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (DB_CLONE_ADVISORY_LOCK_KEY,))
        conn.close()
    except Exception as exc:
        logger.warning("Failed to release DB clone advisory lock: %s", exc)


def _clone_result(
    stats: CloneStats,
    *,
    site_ids: List[int],
    client_id: Optional[int],
    window_start: datetime,
    window_end: datetime,
    window_hours: float,
    dry_run: bool,
) -> Dict[str, Any]:
    return {
        "success": True,
        "dry_run": dry_run,
        "site_ids": site_ids,
        "client_id": client_id,
        "window_start": window_start.isoformat(sep=" "),
        "window_end": window_end.isoformat(sep=" "),
        "window_hours": window_hours,
        "tables": dict(stats.tables),
        "total_rows": sum(stats.tables.values()),
    }


def execute_db_clone(
    site_ids: List[int],
    *,
    client_id: Optional[int] = None,
    vehicle_ids: Optional[List[int]] = None,
    window_start: Optional[datetime] = None,
    window_hours: float = 1.0,
    include_scheduler: bool = True,
    dry_run: bool = False,
    require_lock: bool = True,
) -> Dict[str, Any]:
    """Run a DB clone job, optionally acquiring the shared advisory lock."""
    if not site_ids:
        raise ValueError("site_ids must not be empty")

    if window_start is None:
        window_start, window_end = previous_utc_hour_window()
        window_hours = 1.0
    else:
        window_end = window_start + timedelta(hours=window_hours)

    lock_conn = None
    if require_lock:
        lock_conn = _try_advisory_lock()
        if lock_conn is None:
            raise DbCloneInProgressError("Another DB clone job is already running")

    logger.info(
        "Starting DB clone for sites %s (UTC window %s → %s, dry_run=%s)",
        site_ids,
        window_start,
        window_end,
        dry_run,
    )

    try:
        stats = run_clone(
            site_ids,
            client_id=client_id,
            vehicle_ids=vehicle_ids,
            window_start=window_start,
            window_hours=window_hours,
            include_scheduler=include_scheduler,
            dry_run=dry_run,
        )
        result = _clone_result(
            stats,
            site_ids=site_ids,
            client_id=client_id,
            window_start=window_start,
            window_end=window_end,
            window_hours=window_hours,
            dry_run=dry_run,
        )
        logger.info("DB clone complete: %s total rows", result["total_rows"])
        return result
    finally:
        if lock_conn is not None:
            _release_advisory_lock(lock_conn)


def run_scheduled_hourly_clone() -> None:
    """Clone the previous complete UTC hour for configured sites."""
    site_ids = _configured_site_ids()
    if not site_ids:
        logger.warning("Hourly DB clone skipped: CLONE_SITE_IDS is empty")
        return

    try:
        execute_db_clone(
            site_ids,
            client_id=CLONE_CLIENT_ID,
            require_lock=True,
        )
    except DbCloneInProgressError:
        logger.info("Hourly DB clone skipped: another worker holds the advisory lock")
    except Exception as exc:
        logger.error("Scheduled hourly DB clone failed: %s", exc, exc_info=True)


_scheduler: Optional[BackgroundScheduler] = None


def start_db_clone_scheduler() -> Optional[BackgroundScheduler]:
    """Start the hourly UTC clone scheduler when enabled via env."""
    global _scheduler

    if not CLONE_DB_SCHEDULER_ENABLED:
        logger.info("DB clone scheduler disabled (set CLONE_DB_SCHEDULER_ENABLED=true to enable)")
        return None

    if _scheduler is not None:
        return _scheduler

    site_ids = _configured_site_ids()
    if not site_ids:
        logger.warning(
            "DB clone scheduler not started: CLONE_SITE_IDS is empty"
        )
        return None

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        run_scheduled_hourly_clone,
        trigger=CronTrigger(minute=0, timezone="UTC"),
        id="hourly_db_clone",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.start()
    _scheduler = scheduler

    logger.info(
        "DB clone scheduler started: hourly at :00 UTC for sites %s (client_id=%s)",
        site_ids,
        CLONE_CLIENT_ID,
    )
    return scheduler


def stop_db_clone_scheduler() -> None:
    """Shut down the background scheduler if running."""
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None
    logger.info("DB clone scheduler stopped")
