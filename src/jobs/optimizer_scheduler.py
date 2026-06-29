"""In-process cron scheduler for unified allocation + charge scheduling optimization."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import psycopg2
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config import (
    OPTIMIZER_CRON_HOUR,
    OPTIMIZER_CRON_MINUTE,
    OPTIMIZER_SCHEDULER_ENABLED,
    OPTIMIZER_SITE_IDS,
    SCHEDULED_PERSIST_TO_DATABASE,
    SCHEDULED_WINDOW_HOURS,
)
from src.controllers.unified_controller import UnifiedController
from src.jobs.db_clone import DbConfig, parse_site_ids
from src.optimizer.unified_optimizer import (
    UnifiedOptimizationConfig,
    default_unified_optimization_config,
    resolve_optimization_from_modes,
)
from src.utils.logging_config import logger

# Stable advisory-lock key for cross-worker / cross-instance mutual exclusion.
OPTIMIZER_ADVISORY_LOCK_KEY = 0x4F50544D  # 'OPTM'

SCHEDULED_MODE_FLAGS = ["allocation", "charge_scheduling"]


class OptimizerInProgressError(Exception):
    """Raised when another scheduled optimization job already holds the advisory lock."""


def _configured_site_ids() -> List[int]:
    if not OPTIMIZER_SITE_IDS:
        return []
    return parse_site_ids(OPTIMIZER_SITE_IDS) or []


def _build_scheduled_config() -> UnifiedOptimizationConfig:
    opt_mode, enable_charger_allocation = resolve_optimization_from_modes(SCHEDULED_MODE_FLAGS)
    config = default_unified_optimization_config(opt_mode)
    config.enable_charger_allocation = enable_charger_allocation
    return config


def _try_advisory_lock() -> Optional[psycopg2.extensions.connection]:
    config = DbConfig.from_env("")
    conn = psycopg2.connect(
        user=config.user,
        password=config.password,
        database=config.database,
        host=config.host,
        port=config.port,
    )
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (OPTIMIZER_ADVISORY_LOCK_KEY,))
        acquired = cur.fetchone()[0]
    if not acquired:
        conn.close()
        return None
    return conn


def _release_advisory_lock(conn: psycopg2.extensions.connection) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (OPTIMIZER_ADVISORY_LOCK_KEY,))
        conn.close()
    except Exception as exc:
        logger.warning("Failed to release optimizer advisory lock: %s", exc)


def _run_optimization_for_site(
    site_id: int,
    *,
    current_time: Optional[datetime] = None,
) -> Dict[str, Any]:
    config = _build_scheduled_config()
    controller = UnifiedController(site_id=site_id, trigger_type="initial")
    try:
        allocation_result, schedule_result, unified_result = controller.run_unified_optimization(
            current_time=current_time,
            mode=SCHEDULED_MODE_FLAGS,
            config=config,
            persist_to_database=SCHEDULED_PERSIST_TO_DATABASE,
            window_hours=SCHEDULED_WINDOW_HOURS,
        )
    finally:
        controller.close()

    result: Dict[str, Any] = {
        "success": True,
        "site_id": site_id,
        "mode": SCHEDULED_MODE_FLAGS,
        "allocation_id": getattr(controller, "allocation_id", None),
        "schedule_id": getattr(controller, "schedule_id", None),
        "unified_status": unified_result.status,
        "routes_allocated": unified_result.routes_allocated,
        "routes_total": unified_result.routes_total,
    }
    if allocation_result is not None:
        result["allocation_status"] = allocation_result.status
        result["allocation_score"] = allocation_result.total_score
    if schedule_result is not None:
        result["schedule_status"] = schedule_result.optimization_status
        result["schedule_cost"] = schedule_result.total_cost
    return result


def execute_scheduled_optimization(
    site_id: int,
    *,
    current_time: Optional[datetime] = None,
    require_lock: bool = True,
) -> Dict[str, Any]:
    """Run scheduled unified optimization for one site (same params as /optimize/unified)."""
    lock_conn = None
    if require_lock:
        lock_conn = _try_advisory_lock()
        if lock_conn is None:
            raise OptimizerInProgressError("Another scheduled optimization job is already running")

    scheduled_config = _build_scheduled_config()
    logger.info(
        "Starting scheduled optimization for site %s (mode=%s, window_hours=%s, "
        "allocation_score_weight=%s, scheduling_cost_weight=%s)",
        site_id,
        SCHEDULED_MODE_FLAGS,
        SCHEDULED_WINDOW_HOURS,
        scheduled_config.allocation_score_weight,
        scheduled_config.scheduling_cost_weight,
    )

    try:
        result = _run_optimization_for_site(site_id, current_time=current_time)
    finally:
        if lock_conn is not None:
            _release_advisory_lock(lock_conn)

    logger.info(
        "Scheduled optimization complete for site %s: allocation_id=%s schedule_id=%s status=%s",
        site_id,
        result["allocation_id"],
        result["schedule_id"],
        result["unified_status"],
    )
    return result


def run_scheduled_optimization() -> None:
    """Run unified optimization for all configured sites."""
    site_ids = _configured_site_ids()
    if not site_ids:
        logger.warning("Scheduled optimization skipped: OPTIMIZER_SITE_IDS / CLONE_SITE_IDS is empty")
        return

    lock_conn = _try_advisory_lock()
    if lock_conn is None:
        logger.info("Scheduled optimization skipped: another worker holds the advisory lock")
        return

    logger.info("Scheduled optimization starting for sites %s", site_ids)
    try:
        for site_id in site_ids:
            try:
                result = _run_optimization_for_site(site_id)
                logger.info(
                    "Scheduled optimization complete for site %s: allocation_id=%s schedule_id=%s status=%s",
                    site_id,
                    result["allocation_id"],
                    result["schedule_id"],
                    result["unified_status"],
                )
            except Exception as exc:
                logger.error(
                    "Scheduled optimization failed for site %s: %s",
                    site_id,
                    exc,
                    exc_info=True,
                )
    finally:
        _release_advisory_lock(lock_conn)


_scheduler: Optional[BackgroundScheduler] = None


def start_optimizer_scheduler() -> Optional[BackgroundScheduler]:
    """Start the cron optimizer scheduler when enabled via env."""
    global _scheduler

    if not OPTIMIZER_SCHEDULER_ENABLED:
        logger.info(
            "Optimizer scheduler disabled (set OPTIMIZER_SCHEDULER_ENABLED=true to enable)"
        )
        return None

    if _scheduler is not None:
        return _scheduler

    site_ids = _configured_site_ids()
    if not site_ids:
        logger.warning(
            "Optimizer scheduler not started: OPTIMIZER_SITE_IDS / CLONE_SITE_IDS is empty"
        )
        return None

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        run_scheduled_optimization,
        trigger=CronTrigger(
            minute=OPTIMIZER_CRON_MINUTE,
            hour=OPTIMIZER_CRON_HOUR,
            timezone="UTC",
        ),
        id="scheduled_unified_optimization",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    scheduler.start()
    _scheduler = scheduler

    logger.info(
        "Optimizer scheduler started: cron minute=%s hour=%s UTC for sites %s",
        OPTIMIZER_CRON_MINUTE,
        OPTIMIZER_CRON_HOUR,
        site_ids,
    )
    return scheduler


def stop_optimizer_scheduler() -> None:
    """Shut down the background scheduler if running."""
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None
    logger.info("Optimizer scheduler stopped")
