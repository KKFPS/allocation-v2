"""HTTP API for unified allocation and scheduling optimization.

Run the server from project root:
  uvicorn src.api.unified_api:app --reload --host 0.0.0.0 --port 8000

Endpoints:
  POST /optimize/unified   - body: UnifiedOptimizationRequest (JSON), all params optional except site_id
  GET  /report/schedule   - query: schedule_id, optional timestamp (as-of time for report)
  GET  /health            - health check

Examples:
  # Run unified optimization
  curl -X POST http://localhost:8000/optimize/unified \\
    -H "Content-Type: application/json" \\
    -d '{"site_id": 10, "test_start_time": "2026-02-16 04:30:00", "mode": "integrated"}'

  # Get schedule report (timestamp optional; default is now)
  curl "http://localhost:8000/report/schedule?schedule_id=1"
  curl "http://localhost:8000/report/schedule?schedule_id=1&timestamp=2026-02-16T06:00:00"
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from src.controllers.unified_controller import UnifiedController
from src.optimizer.unified_optimizer import (
    OptimizationMode,
    UnifiedOptimizationConfig,
)


# --- Request body: all optional except site_id, with defaults ---


class UnifiedOptimizationRequest(BaseModel):
    """Request body for unified optimization. All parameters optional except site_id."""

    site_id: int = Field(..., description="Site identifier")
    trigger_type: str = Field("initial", description="Type of allocation trigger")
    schedule_id: Optional[int] = Field(None, description="Existing schedule ID (optional)")

    # Test / override current time (as in test_unified_optimizer.py)
    test_start_time: Optional[str] = Field(
        None,
        description=(
            "Start time for optimization (simulated 'now'). "
            "ISO 8601 (e.g. 2026-02-16T04:30:00) or 'YYYY-MM-DD HH:MM:SS'. "
            "If omitted, current server time is used."
        ),
    )

    mode: str = Field(
        "integrated",
        description="Optimization mode: allocation_only, scheduling_only, integrated",
    )
    persist_to_database: bool = Field(True, description="Whether to persist results to DB")

    # Planning window
    window_hours: float = Field(
        24.0,
        description="Total planning window in hours (default: 24). Overrides site/MAF allocation_window_hours when set.",
    )

    # Optimization config overrides (fallbacks to UnifiedOptimizationConfig defaults)
    allocation_time_limit: Optional[int] = Field(None, description="Allocation phase time limit (seconds)")
    scheduling_time_limit: Optional[int] = Field(None, description="Scheduling phase time limit (seconds)")
    integrated_time_limit: Optional[int] = Field(None, description="Integrated mode time limit (seconds)")
    route_count_weight: Optional[float] = Field(None, description="Weight for route coverage priority")
    allocation_score_weight: Optional[float] = Field(None, description="α: weight for allocation score")
    scheduling_cost_weight: Optional[float] = Field(None, description="β: weight for scheduling cost")
    target_soc_shortfall_penalty: Optional[float] = Field(None, description="λ: penalty per kWh shortfall")
    triad_penalty_factor: Optional[float] = Field(None, description="TRIAD period penalty factor")
    synthetic_time_price_factor: Optional[float] = Field(None, description="Time preference factor")
    target_soc_percent: Optional[float] = Field(None, description="Target SOC percentage")
    site_capacity_kw: Optional[float] = Field(None, description="Site capacity in kW")

    class Config:
        extra = "ignore"


def _parse_start_time(value: str) -> datetime:
    """Parse test_start_time: ISO 8601 or 'YYYY-MM-DD HH:MM:SS'."""
    value = value.strip()
    # Try ISO format first
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    # Try space-separated format (as in test_unified_optimizer.py)
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise ValueError(
            f"Invalid test_start_time: {value!r}. "
            "Use ISO 8601 (e.g. 2026-02-16T04:30:00) or 'YYYY-MM-DD HH:MM:SS'."
        )


def _build_config_from_request(req: UnifiedOptimizationRequest) -> Optional[UnifiedOptimizationConfig]:
    """Build UnifiedOptimizationConfig from request overrides; None if no overrides."""
    mode_str = (req.mode or "integrated").lower()
    mode_map = {
        "allocation_only": OptimizationMode.ALLOCATION_ONLY,
        "allocation": OptimizationMode.ALLOCATION_ONLY,
        "scheduling_only": OptimizationMode.SCHEDULING_ONLY,
        "scheduling": OptimizationMode.SCHEDULING_ONLY,
        "integrated": OptimizationMode.INTEGRATED,
        "both": OptimizationMode.INTEGRATED,
    }
    mode = mode_map.get(mode_str, OptimizationMode.INTEGRATED)

    # Check if any config override was provided
    overrides = [
        req.allocation_time_limit,
        req.scheduling_time_limit,
        req.integrated_time_limit,
        req.route_count_weight,
        req.allocation_score_weight,
        req.scheduling_cost_weight,
        req.target_soc_shortfall_penalty,
        req.triad_penalty_factor,
        req.synthetic_time_price_factor,
        req.target_soc_percent,
        req.site_capacity_kw,
    ]
    if all(o is None for o in overrides):
        return None

    defaults = UnifiedOptimizationConfig(mode=mode)
    return UnifiedOptimizationConfig(
        mode=mode,
        allocation_time_limit=req.allocation_time_limit if req.allocation_time_limit is not None else defaults.allocation_time_limit,
        scheduling_time_limit=req.scheduling_time_limit if req.scheduling_time_limit is not None else defaults.scheduling_time_limit,
        integrated_time_limit=req.integrated_time_limit if req.integrated_time_limit is not None else defaults.integrated_time_limit,
        route_count_weight=req.route_count_weight if req.route_count_weight is not None else defaults.route_count_weight,
        allocation_score_weight=req.allocation_score_weight if req.allocation_score_weight is not None else defaults.allocation_score_weight,
        scheduling_cost_weight=req.scheduling_cost_weight if req.scheduling_cost_weight is not None else defaults.scheduling_cost_weight,
        target_soc_shortfall_penalty=req.target_soc_shortfall_penalty if req.target_soc_shortfall_penalty is not None else defaults.target_soc_shortfall_penalty,
        triad_penalty_factor=req.triad_penalty_factor if req.triad_penalty_factor is not None else defaults.triad_penalty_factor,
        synthetic_time_price_factor=req.synthetic_time_price_factor if req.synthetic_time_price_factor is not None else defaults.synthetic_time_price_factor,
        target_soc_percent=req.target_soc_percent if req.target_soc_percent is not None else defaults.target_soc_percent,
        site_capacity_kw=req.site_capacity_kw if req.site_capacity_kw is not None else defaults.site_capacity_kw,
    )


def _result_to_jsonable(result: Any) -> Dict[str, Any]:
    """Turn UnifiedOptimizationResult and related objects into JSON-serializable dict."""
    from src.optimizer.unified_optimizer import UnifiedOptimizationResult

    if not isinstance(result, UnifiedOptimizationResult):
        return {"raw": str(result)}

    out = {
        "mode": result.mode.value if hasattr(result.mode, "value") else str(result.mode),
        "status": result.status,
        "solve_time_seconds": result.solve_time_seconds,
        "allocation_score": result.allocation_score,
        "routes_allocated": result.routes_allocated,
        "routes_total": result.routes_total,
        "total_charging_cost": result.total_charging_cost,
        "total_energy_kwh": result.total_energy_kwh,
        "objective_value": result.objective_value,
    }
    # Summarize vehicle schedules for JSON (avoid non-serializable types)
    if result.vehicle_schedules:
        out["vehicle_schedules_summary"] = []
        for vs in result.vehicle_schedules:
            summary = {
                "vehicle_id": vs.vehicle_id,
                "schedule_id": vs.schedule_id,
                "initial_soc_kwh": vs.initial_soc_kwh,
                "target_soc_kwh": vs.target_soc_kwh,
                "total_energy_scheduled_kwh": getattr(vs, "total_energy_scheduled_kwh", None),
            }
            if getattr(vs, "charge_slots", None):
                summary["num_slots"] = len(vs.charge_slots)
                summary["slots"] = [
                    {
                        "time_slot": s.time_slot.isoformat() if hasattr(s.time_slot, "isoformat") else str(s.time_slot),
                        "charge_power_kw": s.charge_power_kw,
                    }
                    for s in vs.charge_slots
                ]
            out["vehicle_schedules_summary"].append(summary)
    return out


app = FastAPI(
    title="Unified Optimization API",
    description="Run combined allocation and scheduling optimization via HTTP.",
    version="1.0.0",
)


@app.post(
    "/optimize/unified",
    response_model=Dict[str, Any],
    summary="Run unified optimization",
    description=(
        "Runs the unified controller (allocation and/or charge scheduling). "
        "Accepts all controller and optimizer parameters with fallbacks to defaults. "
        "Optional test_start_time uses the same semantics as tests (simulated current time)."
    ),
)
def run_unified_optimization(body: UnifiedOptimizationRequest) -> Dict[str, Any]:
    current_time: Optional[datetime] = None
    if body.test_start_time:
        current_time = _parse_start_time(body.test_start_time)

    config = _build_config_from_request(body)

    controller = UnifiedController(
        site_id=body.site_id,
        trigger_type=body.trigger_type,
        schedule_id=body.schedule_id,
    )
    try:
        allocation_result, schedule_result, unified_result = controller.run_unified_optimization(
            current_time=current_time,
            mode=body.mode,
            config=config,
            persist_to_database=body.persist_to_database,
            window_hours=body.window_hours,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        controller.close()

    response: Dict[str, Any] = {
        "success": True,
        "unified_result": _result_to_jsonable(unified_result),
        "allocation_id": getattr(controller, "allocation_id", None),
        "schedule_id": getattr(controller, "schedule_id", None),
    }
    if allocation_result is not None:
        response["allocation"] = {
            "allocation_id": allocation_result.allocation_id,
            "status": allocation_result.status,
            "total_score": allocation_result.total_score,
            "routes_in_window": allocation_result.routes_in_window,
            "routes_allocated": allocation_result.routes_allocated,
            "acceptable": allocation_result.is_acceptable(),
        }
    if schedule_result is not None:
        response["schedule"] = {
            "schedule_id": schedule_result.schedule_id,
            "optimization_status": schedule_result.optimization_status,
            "total_cost": schedule_result.total_cost,
            "total_energy_kwh": schedule_result.total_energy_kwh,
            "vehicles_scheduled": schedule_result.vehicles_scheduled,
        }
    return response


@app.get(
    "/report/schedule",
    response_model=Dict[str, Any],
    summary="Get schedule report",
    description=(
        "Returns a read-only report for a persisted schedule: charging/allocation stats, "
        "charging time before first route and between routes, end-of-plan SOC, and per-vehicle details. "
        "timestamp is the as-of time for vehicle state (default: now)."
    ),
)
def get_schedule_report(
    schedule_id: int = Query(..., description="Schedule ID from t_scheduler"),
    timestamp: Optional[str] = Query(
        None,
        description="As-of time for report (ISO 8601 or 'YYYY-MM-DD HH:MM:SS'). Default: now.",
    ),
) -> Dict[str, Any]:
    if timestamp is None:
        report_timestamp = datetime.now()
    else:
        try:
            report_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            try:
                report_timestamp = datetime.strptime(timestamp.strip(), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid timestamp. Use ISO 8601 (e.g. 2026-02-16T06:00:00) or 'YYYY-MM-DD HH:MM:SS'.",
                )

    controller = UnifiedController(site_id=0)
    try:
        report = controller.get_schedule_report(schedule_id, report_timestamp)
        return report.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        controller.close()


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}
