"""Scheduler data models for charge scheduling."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict
from enum import Enum


class ScheduleType(Enum):
    """Schedule types."""
    OVERNIGHT = 'overnight'
    DAYTIME = 'daytime'
    DYNAMIC = 'dynamic'


class RouteSourceMode(Enum):
    """Route source modes for vehicle-route mapping."""
    ROUTE_PLAN_ONLY = 'route_plan_only'  # Use t_route_plan with vehicle_id
    ALLOCATED_ROUTES = 'allocated_routes'  # Use t_route_plan JOIN t_route_allocated


@dataclass
class SchedulerConfig:
    """Configuration for a scheduling run."""
    
    schedule_id: Optional[int] = None
    site_id: Optional[int] = None
    schedule_type: str = 'dynamic'
    status: str = 'pending'
    
    # Planning window configuration
    planning_window_hours: float = 24.0
    route_energy_safety_factor: float = 1.15
    min_departure_buffer_minutes: int = 60
    back_to_back_threshold_minutes: int = 90
    
    # Target SOC configuration
    target_soc_percent: float = 75.0
    min_soc_percent: float = 75.0  # Minimum charge level (e.g. charge at least to 75%)
    battery_factor: float = 1.0  # Max SOC multiplier
    
    # Site capacity configuration
    agreed_site_capacity_kva: Optional[float] = None
    power_factor: float = 0.85
    site_usage_factor: float = 0.90
    
    # Fast charger configuration
    max_fast_chargers: int = 0
    
    # Optimization parameters
    time_limit_seconds: int = 300
    triad_penalty_factor: float = 100.0
    synthetic_time_price_factor: float = 0.01
    
    # Timestamps
    created_date_time: Optional[datetime] = None
    run_datetime: Optional[datetime] = None
    actual_planning_window_hours: Optional[float] = None
    
    # Route source configuration
    route_source_mode: RouteSourceMode = RouteSourceMode.ROUTE_PLAN_ONLY

    @property
    def site_capacity_kw(self) -> float:
        """Site capacity in kW for optimization (from agreed_site_capacity_kva * power_factor * site_usage_factor)."""
        if self.agreed_site_capacity_kva is None:
            return 0.0
        return self.agreed_site_capacity_kva * self.power_factor * self.site_usage_factor
    
    def validate(self) -> List[str]:
        """
        Validate configuration parameters.
        
        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []
        
        if not (4.0 <= self.planning_window_hours <= 24.0):
            errors.append(f"planning_window_hours must be between 4.0 and 24.0, got {self.planning_window_hours}")
        
        if not (1.0 <= self.route_energy_safety_factor <= 2.0):
            errors.append(f"route_energy_safety_factor must be between 1.0 and 2.0, got {self.route_energy_safety_factor}")
        
        if not (15 <= self.min_departure_buffer_minutes <= 180):
            errors.append(f"min_departure_buffer_minutes must be between 15 and 180, got {self.min_departure_buffer_minutes}")
        
        if not (30 <= self.back_to_back_threshold_minutes <= 240):
            errors.append(f"back_to_back_threshold_minutes must be between 30 and 240, got {self.back_to_back_threshold_minutes}")
        
        if not (50.0 <= self.target_soc_percent <= 100.0):
            errors.append(f"target_soc_percent must be between 50.0 and 100.0, got {self.target_soc_percent}")
        
        if not (0.0 <= self.min_soc_percent <= 100.0):
            errors.append(f"min_soc_percent must be between 0.0 and 100.0, got {self.min_soc_percent}")
        if self.min_soc_percent > self.target_soc_percent:
            errors.append(f"min_soc_percent ({self.min_soc_percent}) must be <= target_soc_percent ({self.target_soc_percent})")
        
        return errors


@dataclass
class VehicleChargeState:
    """Current charging state for a vehicle."""
    
    vehicle_id: int
    current_soc_percent: float
    current_soc_kwh: float
    battery_capacity_kwh: float
    
    # Connection state
    is_connected: bool
    charger_id: Optional[int] = None
    charger_type: Optional[str] = None  # 'AC' or 'DC'
    
    # Vehicle capabilities
    ac_charge_rate_kw: float = 11.0
    dc_charge_rate_kw: float = 50.0
    efficiency_kwh_mile: Optional[float] = None
    
    # Current status
    status: Optional[str] = None  # 'Idle', 'On-Route', 'Charging', 'VOR'
    current_route_id: Optional[str] = None
    return_eta: Optional[datetime] = None
    return_soc_percent: Optional[float] = None
    
    @property
    def missing_energy_kwh(self) -> float:
        """Calculate energy needed to reach 100% SOC."""
        return self.battery_capacity_kwh - self.current_soc_kwh
    
    def is_available_for_charging(self) -> bool:
        """Check if vehicle can be charged."""
        return self.status != 'VOR' and self.is_connected


@dataclass
class RouteEnergyRequirement:
    """Energy requirement for a specific route."""
    
    route_id: str
    vehicle_id: int
    plan_start_date_time: datetime
    plan_end_date_time: datetime
    plan_mileage: float
    route_status: str
    
    # Energy calculations
    efficiency_kwh_mile: float
    route_energy_buffer_kwh: float
    cumulative_energy_kwh: float
    
    # Route metadata
    route_sequence_index: int = 0  # Position in sequence (0 = first route)
    is_back_to_back: bool = False
    gap_to_next_minutes: Optional[float] = None


@dataclass
class VehicleAvailability:
    """Time-slotted availability for a vehicle."""
    
    vehicle_id: int
    time_slots: List[datetime]
    availability_matrix: List[bool]  # True = available for charging
    
    # Unavailability reasons (for logging)
    unavailable_periods: List[Dict[str, any]] = field(default_factory=list)
    
    def is_available_at(self, time_slot: datetime) -> bool:
        """Check if vehicle is available at specific time."""
        try:
            idx = self.time_slots.index(time_slot)
            return self.availability_matrix[idx]
        except (ValueError, IndexError):
            return False
    
    def get_availability_window(self, start_time: datetime, end_time: datetime) -> List[bool]:
        """Get availability for a time window."""
        result = []
        for i, slot_time in enumerate(self.time_slots):
            if start_time <= slot_time < end_time:
                result.append(self.availability_matrix[i])
        return result


@dataclass
class ChargeScheduleResult:
    """Result of charge schedule optimization."""
    
    schedule_id: int
    site_id: int
    vehicle_schedules: List['VehicleChargeSchedule']
    
    # Planning window
    planning_start: datetime
    planning_end: datetime
    actual_planning_window_hours: float
    
    # Optimization metrics
    total_cost: float
    total_energy_kwh: float
    solve_time_seconds: float
    optimization_status: str
    
    # Validation results
    validation_passed: bool
    validation_errors: List[str] = field(default_factory=list)
    validation_warnings: List[str] = field(default_factory=list)
    
    # Statistics
    vehicles_scheduled: int = 0
    routes_considered: int = 0
    checkpoints_created: int = 0
    
    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            'schedule_id': self.schedule_id,
            'site_id': self.site_id,
            'planning_start': self.planning_start.isoformat(),
            'planning_end': self.planning_end.isoformat(),
            'actual_planning_window_hours': self.actual_planning_window_hours,
            'total_cost': self.total_cost,
            'total_energy_kwh': self.total_energy_kwh,
            'solve_time_seconds': self.solve_time_seconds,
            'optimization_status': self.optimization_status,
            'validation_passed': self.validation_passed,
            'vehicles_scheduled': self.vehicles_scheduled,
            'routes_considered': self.routes_considered,
            'checkpoints_created': self.checkpoints_created,
            'vehicle_schedules': [vs.to_dict() for vs in self.vehicle_schedules]
        }


@dataclass
class VehicleChargeSchedule:
    """Charging schedule for a single vehicle."""
    
    vehicle_id: int
    schedule_id: int
    
    # Energy requirements
    initial_soc_kwh: float
    target_soc_kwh: float
    total_energy_needed_kwh: float
    
    # Route requirements
    route_checkpoints: List[RouteEnergyRequirement] = field(default_factory=list)
    has_routes: bool = False
    
    # Charging plan
    charge_slots: List['ChargeSlot'] = field(default_factory=list)
    total_energy_scheduled_kwh: float = 0.0
    
    # Charger assignment
    assigned_charger_id: Optional[int] = None
    charger_type: Optional[str] = None
    
    # Validation
    meets_route_requirements: bool = True
    energy_shortfall_kwh: float = 0.0
    
    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            'vehicle_id': self.vehicle_id,
            'schedule_id': self.schedule_id,
            'initial_soc_kwh': self.initial_soc_kwh,
            'target_soc_kwh': self.target_soc_kwh,
            'total_energy_needed_kwh': self.total_energy_needed_kwh,
            'has_routes': self.has_routes,
            'total_energy_scheduled_kwh': self.total_energy_scheduled_kwh,
            'assigned_charger_id': self.assigned_charger_id,
            'charger_type': self.charger_type,
            'meets_route_requirements': self.meets_route_requirements,
            'energy_shortfall_kwh': self.energy_shortfall_kwh,
            'route_checkpoints': len(self.route_checkpoints),
            'charge_slots': len(self.charge_slots)
        }


@dataclass
class ChargeSlot:
    """Charging power allocation for a specific time slot."""
    
    time_slot: datetime
    charge_power_kw: float
    cumulative_energy_kwh: float
    electricity_price: float
    
    # Site context
    site_demand_kw: Optional[float] = None
    site_capacity_available_kw: Optional[float] = None
    is_triad_period: bool = False
    
    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            'time_slot': self.time_slot.isoformat(),
            'charge_power_kw': self.charge_power_kw,
            'cumulative_energy_kwh': self.cumulative_energy_kwh,
            'electricity_price': self.electricity_price,
            'is_triad_period': self.is_triad_period
        }


@dataclass
class ForecastDataHorizon:
    """Data availability horizon for forecasts and prices."""
    
    current_time: datetime
    max_forecast_timestamp: Optional[datetime] = None
    max_price_timestamp: Optional[datetime] = None
    
    @property
    def effective_horizon(self) -> Optional[datetime]:
        """Get the earliest limiting timestamp."""
        horizons = [h for h in [self.max_forecast_timestamp, self.max_price_timestamp] if h is not None]
        return min(horizons) if horizons else None
    
    @property
    def forecast_hours_available(self) -> Optional[float]:
        """Hours of forecast data available."""
        if self.max_forecast_timestamp:
            delta = self.max_forecast_timestamp - self.current_time
            return max(0, delta.total_seconds() / 3600.0)
        return None
    
    @property
    def price_hours_available(self) -> Optional[float]:
        """Hours of price data available."""
        if self.max_price_timestamp:
            delta = self.max_price_timestamp - self.current_time
            return max(0, delta.total_seconds() / 3600.0)
        return None
