"""Vehicle data model."""
from dataclasses import dataclass
from typing import Optional
from datetime import datetime


@dataclass
class Vehicle:
    """Represents a delivery vehicle."""
    
    vehicle_id: int
    site_id: int
    active: bool
    VOR: bool
    charge_power_ac: float
    charge_power_dc: float
    battery_capacity: float
    efficiency_kwh_mile: float
    telematic_label: Optional[str] = None
    
    # Runtime state (from VSM)
    current_status: Optional[str] = None
    current_route_id: Optional[str] = None
    estimated_soc: Optional[float] = None
    return_eta: Optional[datetime] = None
    return_soc: Optional[float] = None
    
    # Availability calculations
    available_time: Optional[datetime] = None
    available_energy_kwh: Optional[float] = None
    
    # Current charger
    current_charger_id: Optional[int] = None
    
    def is_available_for_allocation(self) -> bool:
        """Check if vehicle can be allocated."""
        return self.active and not self.vor
    
    def get_available_energy(self, current_time: datetime) -> float:
        """
        Calculate available energy in kWh.
        
        Args:
            current_time: Current datetime
        
        Returns:
            Available energy in kWh
        """
        if self.estimated_soc is not None:
            return (self.estimated_soc / 100.0) * self.battery_capacity
        elif self.return_soc is not None:
            return (self.return_soc / 100.0) * self.battery_capacity
        else:
            # Assume full charge if no data
            return self.battery_capacity
    
    def calculate_energy_required(self, distance_miles: float) -> float:
        """
        Calculate energy required for a given distance.
        
        Args:
            distance_miles: Distance in miles
        
        Returns:
            Energy required in kWh
        """
        return distance_miles * self.efficiency_kwh_mile
    
    def calculate_charging_time(self, energy_needed_kwh: float, use_dc: bool = False) -> float:
        """
        Calculate charging time in hours.
        
        Args:
            energy_needed_kwh: Energy to charge in kWh
            use_dc: Whether to use DC charging
        
        Returns:
            Charging time in hours
        """
        if energy_needed_kwh <= 0:
            return 0.0
        
        charge_power = self.charge_power_dc if use_dc else self.charge_power_ac
        return energy_needed_kwh / charge_power if charge_power > 0 else float('inf')
    
    def __repr__(self):
        return f"Vehicle(id={self.vehicle_id}, label={self.telematic_label}, soc={self.estimated_soc}%)"
