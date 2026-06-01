"""Schedule report models (read-only reporting from persisted schedules)."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class VehicleScheduleReport:
    """Per-vehicle summary for a schedule report."""

    vehicle_id: int
    initial_soc_kwh: Optional[float] = None
    initial_soc_percent: Optional[float] = None
    battery_capacity_kwh: Optional[float] = None
    total_energy_scheduled_kwh: float = 0.0
    charging_minutes_before_first_route: Optional[float] = None
    charging_minutes_between_routes: List[float] = field(default_factory=list)
    total_charging_minutes_between_routes: float = 0.0
    estimated_final_soc_kwh: Optional[float] = None
    estimated_final_soc_percent: Optional[float] = None
    energy_required_for_routes_kwh: float = 0.0
    charge_rate_kw: Optional[float] = None
    assigned_charger_power_kw: Optional[float] = None
    allocated_route_ids: List[str] = field(default_factory=list)
    routes_allocated_count: int = 0
    allocated_routes: List[Dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "vehicle_id": self.vehicle_id,
            "initial_soc_kwh": self.initial_soc_kwh,
            "initial_soc_percent": self.initial_soc_percent,
            "battery_capacity_kwh": self.battery_capacity_kwh,
            "total_energy_scheduled_kwh": self.total_energy_scheduled_kwh,
            "charging_minutes_before_first_route": self.charging_minutes_before_first_route,
            "charging_minutes_between_routes": self.charging_minutes_between_routes,
            "total_charging_minutes_between_routes": self.total_charging_minutes_between_routes,
            "estimated_final_soc_kwh": self.estimated_final_soc_kwh,
            "estimated_final_soc_percent": self.estimated_final_soc_percent,
            "energy_required_for_routes_kwh": self.energy_required_for_routes_kwh,
            "charge_rate_kw": self.charge_rate_kw,
            "assigned_charger_power_kw": self.assigned_charger_power_kw,
            "allocated_route_ids": self.allocated_route_ids,
            "routes_allocated_count": self.routes_allocated_count,
            "allocated_routes": self.allocated_routes,
        }


@dataclass
class ScheduleReport:
    """Result of get_schedule_report(schedule_id, timestamp)."""

    schedule_id: int
    site_id: int
    report_timestamp: datetime
    planning_start: Optional[datetime] = None
    planning_end: Optional[datetime] = None
    schedule_status: Optional[str] = None
    vehicles_scheduled: int = 0
    total_energy_scheduled_kwh: float = 0.0
    routes_in_window: int = 0
    routes_allocated: Optional[int] = None
    vehicles_with_routes: int = 0
    total_charging_minutes_fleet: float = 0.0
    vehicle_reports: List[VehicleScheduleReport] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schedule_id": self.schedule_id,
            "site_id": self.site_id,
            "report_timestamp": self.report_timestamp.isoformat(),
            "planning_start": self.planning_start.isoformat() if self.planning_start else None,
            "planning_end": self.planning_end.isoformat() if self.planning_end else None,
            "schedule_status": self.schedule_status,
            "vehicles_scheduled": self.vehicles_scheduled,
            "total_energy_scheduled_kwh": self.total_energy_scheduled_kwh,
            "routes_in_window": self.routes_in_window,
            "routes_allocated": self.routes_allocated,
            "vehicles_with_routes": self.vehicles_with_routes,
            "total_charging_minutes_fleet": self.total_charging_minutes_fleet,
            "vehicle_reports": [vr.to_dict() for vr in self.vehicle_reports],
            "notes": self.notes,
        }
