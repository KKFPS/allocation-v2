"""HTTP API for unified route allocation and charge scheduling."""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union

import traceback
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, validator

from src.controllers.unified_controller import UnifiedController
from src.integrations.microlise import MicroLiseClient, MicroLiseParams
from src.optimizer.unified_optimizer import OptimizationConfig, OptimizationResult
from src.utils.logging_config import logger


class MicroliseConnectionType(str, Enum):
    test = "test"
    prod = "prod"


class UnifiedOptimizationRequest(BaseModel):
    """Request body for unified optimization."""

    site_id: int = Field(..., description="Site identifier")
    trigger_type: str = Field("initial", description="Allocation trigger type")
    schedule_id: Optional[int] = Field(
        None, description="Existing schedule ID for charge_scheduling mode"
    )
    mode: List[str] = Field(
        default_factory=lambda: ["allocation"],
        description="Modes: allocation, charge_scheduling, charger_allocation (Phase 3)",
    )
    test_start_time: Optional[datetime] = Field(
        None,
        description="Simulated current time (ISO 8601 or YYYY-MM-DD HH:MM:SS)",
    )
    persist_to_database: bool = Field(True, description="Persist results to DB")
    window_hours: Optional[float] = Field(
        None,
        gt=0,
        description="Planning window hours (overrides MAF default)",
    )
    time_limit_seconds: Optional[int] = Field(
        None,
        gt=0,
        description="Hexaly solve time limit in seconds",
    )
    p_fixed_kw: Optional[float] = Field(
        None,
        gt=0,
        description="Homogeneous charge power (kW) for integrated scheduling",
    )
    soc_shortfall_penalty: Optional[float] = Field(
        None,
        ge=0,
        description="Soft penalty per kWh SOC shortfall vs route energy / target SOC",
    )
    target_soc_percent: Optional[float] = Field(
        None,
        gt=0,
        le=100,
        description="Target SOC (%) for soft shortfall penalty",
    )

    microlise_enabled: bool = Field(False, description="Dispatch to Microlise after allocation")
    microlise_simulate: bool = Field(True, description="Simulate Microlise API responses")
    microlise_connection_type: str = Field("test", description="'test' or 'prod'")
    microlise_send_report: bool = Field(False, description="Upload Excel report to blob storage")
    microlise_initial_report: bool = Field(True)
    microlise_compliance_report: bool = Field(False)
    microlise_unallocated_report: bool = Field(False)
    microlise_start_hour_allocation: int = Field(6, ge=0, le=23)
    microlise_end_hour_allocation: int = Field(4, ge=0, le=23)

    class Config:
        extra = "ignore"

    @validator("test_start_time", pre=True)
    def _parse_test_start_time(cls, value):
        if value is None or isinstance(value, datetime):
            return value
        value = value.strip()
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise ValueError(
                f"Invalid test_start_time: {value!r}. "
                "Use ISO 8601 or YYYY-MM-DD HH:MM:SS."
            ) from exc

    @validator("mode", pre=True)
    def _validate_mode(cls, value):
        if value is None:
            return ["allocation"]
        if isinstance(value, str):
            return [value]
        normalized = []
        for m in value:
            key = (m or "").strip().lower()
            if key and key not in normalized:
                normalized.append(key)
        return normalized or ["allocation"]


def _build_config_from_request(req: UnifiedOptimizationRequest) -> OptimizationConfig:
    from src.config import UNIFIED_ALLOCATION_TIME_LIMIT, UNIFIED_SOC_SHORTFALL_PENALTY

    kwargs = {
        "time_limit_seconds": (
            req.time_limit_seconds
            if req.time_limit_seconds is not None
            else UNIFIED_ALLOCATION_TIME_LIMIT
        ),
        "p_fixed_kw": req.p_fixed_kw,
    }
    if req.soc_shortfall_penalty is not None:
        kwargs["soc_shortfall_penalty"] = req.soc_shortfall_penalty
    else:
        kwargs["soc_shortfall_penalty"] = UNIFIED_SOC_SHORTFALL_PENALTY
    if req.target_soc_percent is not None:
        kwargs["target_soc_percent"] = req.target_soc_percent
    return OptimizationConfig(**kwargs)


def _solver_result_to_json(
    result: Union[OptimizationResult, Any],
) -> Dict[str, Any]:
    vehicle_routes = getattr(result, "vehicle_route_sequences", None)
    if vehicle_routes is None:
        vehicle_routes = getattr(result, "vehicle_sequences", {})
    charge_slots = getattr(result, "charge_slots_assigned", {}) or {}
    return {
        "status": result.status,
        "solve_time_seconds": result.solve_time_seconds,
        "objective_value": result.objective_value,
        "allocation_score": result.allocation_score,
        "routes_allocated": result.routes_allocated,
        "routes_total": result.routes_total,
        "vehicle_route_sequences": {
            str(v): seq for v, seq in vehicle_routes.items()
        },
        "charge_slots_assigned": {
            str(v): slots for v, slots in charge_slots.items()
        },
    }


app = FastAPI(
    title="Unified Allocation API",
    description="Route allocation and homogeneous charge scheduling (Hexaly).",
    version="2.1.0",
)


@app.post("/optimize/unified", response_model=Dict[str, Any], summary="Run unified optimization")
def run_unified_optimization(body: UnifiedOptimizationRequest) -> Dict[str, Any]:
    config = _build_config_from_request(body)
    controller = UnifiedController(
        site_id=body.site_id,
        trigger_type=body.trigger_type,
        schedule_id=body.schedule_id,
    )
    try:
        allocation_result, schedule_result, solver_result = controller.run_unified_optimization(
            current_time=body.test_start_time,
            mode=body.mode,
            config=config,
            persist_to_database=body.persist_to_database,
            window_hours=body.window_hours,
            p_fixed_kw=body.p_fixed_kw,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        controller.close()

    response: Dict[str, Any] = {
        "success": True,
        "mode": body.mode,
        "allocation_id": controller.allocation_id,
        "schedule_id": controller.schedule_id,
    }

    if solver_result is not None:
        response["optimization_result"] = _solver_result_to_json(solver_result)

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
            "site_id": schedule_result.site_id,
            "planning_start": schedule_result.planning_start.isoformat(),
            "planning_end": schedule_result.planning_end.isoformat(),
            "vehicles_scheduled": len(schedule_result.vehicle_schedules),
        }

    if body.microlise_enabled and response.get("allocation_id") is not None:
        microlise_params = MicroLiseParams(
            simulate_response=body.microlise_simulate,
            send_report=body.microlise_send_report,
            trigger_type=body.trigger_type,
            initial_report=body.microlise_initial_report,
            compliance_report=body.microlise_compliance_report,
            unallocated_report=body.microlise_unallocated_report,
            start_hour_allocation=body.microlise_start_hour_allocation,
            end_hour_allocation=body.microlise_end_hour_allocation,
        )
        microlise_client = MicroLiseClient(connection_type=body.microlise_connection_type)
        try:
            response["microlise"] = microlise_client.run(
                allocation_id=response["allocation_id"],
                site_id=body.site_id,
                params=microlise_params,
            )
        except Exception as exc:
            traceback.print_exc()
            logger.error("Microlise dispatch failed: %s", exc)
            response["microlise"] = {"success": False, "error": str(exc)}

    return response


@app.get("/report/schedule", response_model=Dict[str, Any], summary="Get schedule report")
def get_schedule_report(
    schedule_id: int = Query(..., description="Schedule ID from t_scheduler"),
    timestamp: Optional[datetime] = Query(None, description="As-of time for report"),
) -> Dict[str, Any]:
    report_timestamp = timestamp or datetime.now()
    controller = UnifiedController(site_id=0)
    try:
        report = controller.get_schedule_report(schedule_id, report_timestamp)
        return report.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    finally:
        controller.close()


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}
