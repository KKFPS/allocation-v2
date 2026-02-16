"""Energy feasibility constraint."""
from typing import List
from src.constraints.base import BaseConstraint
from src.models.vehicle import Vehicle
from src.models.route import Route
from src.utils.logging_config import logger


class EnergyFeasibilityConstraint(BaseConstraint):
    """Ensures vehicle has sufficient energy for route sequence."""
    
    def evaluate(self, vehicle: Vehicle, route_sequence: List[Route], **kwargs) -> float:
        """
        Evaluate energy feasibility for route sequence.
        
        Args:
            vehicle: Vehicle being evaluated
            route_sequence: Sequence of routes
            **kwargs: May contain 'site_chargers' with charging infrastructure.
            Uses vehicle.available_time as start of charge window before first route.
        
        Returns:
            Penalty if energy insufficient, 0 otherwise
        """
        if not self.enabled or not route_sequence:
            return 0.0
        
        safety_margin_kwh = self.params.get('safety_margin_kwh', 5.0)
        allow_dc_charging = self.params.get('allow_dc_charging', True)
        
        # Start with current vehicle energy
        current_energy = vehicle.available_energy_kwh or vehicle.battery_capacity
        
        logger.debug(f"    Energy check: start={current_energy:.1f} kWh, safety_margin={safety_margin_kwh} kWh")
        
        # Add charging between now and start of first route (continuous time)
        first_route = route_sequence[0]
        charge_start = vehicle.available_time
        if charge_start is not None:
            time_before_first = (first_route.plan_start_date_time - charge_start).total_seconds() / 3600.0
            time_before_first = max(0.0, time_before_first)
            if time_before_first > 0:
                charger_max_power = None
                site_chargers = kwargs.get("site_chargers") or []
                if vehicle.current_charger_id is not None and site_chargers:
                    for ch in site_chargers:
                        if ch.get("charger_id") == vehicle.current_charger_id:
                            charger_max_power = ch.get("max_power")
                            break
                charge_power = vehicle.get_charge_power(
                    use_dc=allow_dc_charging, charger_max_power=charger_max_power
                )
                potential_charge = time_before_first * charge_power
                current_energy = min(current_energy + potential_charge, vehicle.battery_capacity)
        
        for route in route_sequence:
            # Calculate energy required for this route
            required_energy = vehicle.calculate_energy_required(route.plan_mileage)
            logger.debug(f"      Route {route.route_id}: need {required_energy:.1f} kWh, have {current_energy:.1f} kWh")
            
            # Check if we have enough energy (with safety margin)
            if current_energy < (required_energy + safety_margin_kwh):
                return self.penalty
            
            # Deduct energy used
            current_energy -= required_energy
            
            # Add charging between routes if time available
            if route != route_sequence[-1]:
                next_route = route_sequence[route_sequence.index(route) + 1]
                time_between = (next_route.plan_start_date_time - route.plan_end_date_time).total_seconds() / 3600.0
                
                # Calculate potential charging (min of vehicle rate and charger max_power)
                if time_between > 0:
                    charger_max_power = None
                    site_chargers = kwargs.get("site_chargers") or []
                    if vehicle.current_charger_id is not None and site_chargers:
                        for ch in site_chargers:
                            if ch.get("charger_id") == vehicle.current_charger_id:
                                charger_max_power = ch.get("max_power")
                                break
                    charge_power = vehicle.get_charge_power(
                        use_dc=allow_dc_charging, charger_max_power=charger_max_power
                    )
                    potential_charge = time_between * charge_power
                    current_energy = min(current_energy + potential_charge, vehicle.battery_capacity)
        
        return 0.0
    
    def is_hard_constraint(self) -> bool:
        """Energy feasibility is a hard constraint."""
        return True
