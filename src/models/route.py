"""Route data model."""
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timedelta


@dataclass
class Route:
    """Represents a delivery route."""
    
    route_id: str
    site_id: int
    route_alias: str
    route_status: str
    plan_start_date_time: datetime
    plan_end_date_time: datetime
    plan_mileage: float
    n_orders: int
    vehicle_id: Optional[int] = None
    actual_start_date_time: Optional[datetime] = None
    actual_end_date_time: Optional[datetime] = None
    energy_kwh: Optional[float] = None  # From scheduling/allocated route data when available
    
    @property
    def duration_hours(self) -> float:
        """Get planned route duration in hours."""
        delta = self.plan_end_date_time - self.plan_start_date_time
        return delta.total_seconds() / 3600.0
    
    @property
    def duration_minutes(self) -> float:
        """Get planned route duration in minutes."""
        delta = self.plan_end_date_time - self.plan_start_date_time
        return delta.total_seconds() / 60.0
    
    def overlaps_with(self, other_route: 'Route', turnaround_minutes: int = 0) -> bool:
        """
        Check if this route overlaps with another route in time.
        
        Args:
            other_route: Another route to check overlap
            turnaround_minutes: Minimum turnaround time between routes
        
        Returns:
            True if routes overlap, False otherwise
        """
        turnaround_delta = timedelta(minutes=turnaround_minutes)
        
        # Check if this route ends before other starts (with turnaround)
        if self.plan_end_date_time + turnaround_delta <= other_route.plan_start_date_time:
            return False
        
        # Check if other route ends before this starts (with turnaround)
        if other_route.plan_end_date_time + turnaround_delta <= self.plan_start_date_time:
            return False
        
        # Routes overlap
        return True
    
    def can_be_sequenced_before(self, next_route: 'Route', turnaround_minutes: int = 45) -> bool:
        """
        Check if this route can be sequenced before another route.
        
        Args:
            next_route: Route to check if it can follow this route
            turnaround_minutes: Minimum turnaround time
        
        Returns:
            True if routes can be sequenced, False otherwise
        """
        turnaround_delta = timedelta(minutes=turnaround_minutes)
        return self.plan_end_date_time + turnaround_delta <= next_route.plan_start_date_time
    
    def is_energy_feasible(self, vehicle, safety_margin_kwh: float = 5.0) -> bool:
        """
        Check if vehicle has sufficient energy for this route.
        
        Args:
            vehicle: Vehicle object
            safety_margin_kwh: Safety margin in kWh
        
        Returns:
            True if feasible, False otherwise
        """
        required_energy = vehicle.calculate_energy_required(self.plan_mileage)
        available_energy = vehicle.available_energy_kwh or vehicle.battery_capacity
        
        return available_energy >= (required_energy + safety_margin_kwh)
    
    def calculate_return_soc(self, vehicle, start_soc_pct: float = 100.0) -> float:
        """
        Calculate expected SOC at route end.
        
        Args:
            vehicle: Vehicle object
            start_soc_pct: Starting SOC percentage
        
        Returns:
            Expected SOC percentage at return
        """
        required_energy = vehicle.calculate_energy_required(self.plan_mileage)
        start_energy = (start_soc_pct / 100.0) * vehicle.battery_capacity
        remaining_energy = start_energy - required_energy
        return (remaining_energy / vehicle.battery_capacity) * 100.0
    
    def __repr__(self):
        return f"Route(id={self.route_id}, alias={self.route_alias}, start={self.plan_start_date_time}, miles={self.plan_mileage})"
