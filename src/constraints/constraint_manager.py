"""Constraint manager for coordinating all constraints."""
from typing import List, Dict
from src.constraints.base import BaseConstraint
from src.constraints.energy_feasibility import EnergyFeasibilityConstraint
from src.constraints.turnaround_time import TurnaroundTimeStrictConstraint, TurnaroundTimePreferredConstraint
from src.constraints.shift_hours import ShiftHoursStrictConstraint
from src.constraints.route_overlap import RouteOverlapConstraint
from src.constraints.charger_preference import ChargerPreferenceConstraint
from src.models.vehicle import Vehicle
from src.models.route import Route
from src.utils.logging_config import logger


class ConstraintManager:
    """Manages and evaluates all allocation constraints."""
    
    def __init__(self, constraint_configs: Dict[str, Dict]):
        """
        Initialize constraint manager.
        
        Args:
            constraint_configs: Dictionary mapping constraint names to configurations
        """
        self.constraints: List[BaseConstraint] = []
        self._initialize_constraints(constraint_configs)
    
    def _initialize_constraints(self, configs: Dict[str, Dict]):
        """
        Initialize all constraint instances.
        
        Args:
            configs: Constraint configurations from MAF
        """
        # Map constraint names to classes
        constraint_classes = {
            'energy_feasibility': EnergyFeasibilityConstraint,
            'turnaround_time_strict': TurnaroundTimeStrictConstraint,
            'turnaround_time_preferred': TurnaroundTimePreferredConstraint,
            'shift_hours_strict': ShiftHoursStrictConstraint,
            'route_overlap': RouteOverlapConstraint,
            'charger_preference': ChargerPreferenceConstraint,
        }

        logger.info(f"Constraint classes: {constraint_classes}")
        logger.info(f"Configs: {configs}")
        
        for name, constraint_class in constraint_classes.items():
            config = configs.get(name, {'enabled': True, 'params': {}, 'penalty': -20})
            
            # Route overlap is always enabled (mandatory)
            if name == 'route_overlap':
                config['enabled'] = True
            
            constraint = constraint_class(config)
            self.constraints.append(constraint)
            
            if constraint.enabled:
                logger.info(f"Initialized constraint: {constraint}")
    
    def evaluate_sequence(self, vehicle: Vehicle, route_sequence: List[Route], **kwargs) -> Dict:
        """
        Evaluate all constraints for a vehicle-route sequence.
        
        Args:
            vehicle: Vehicle being evaluated
            route_sequence: Sequence of routes
            **kwargs: Additional context
        
        Returns:
            Dictionary with total_cost, breakdown, and feasibility
        """
        total_cost = 0.0
        breakdown = {}
        is_feasible = True
        
        route_ids = [r.route_id for r in route_sequence]
        logger.debug(f"Evaluating vehicle {vehicle.vehicle_id} with {len(route_sequence)} routes: {route_ids}")
        
        for constraint in self.constraints:
            if not constraint.enabled:
                continue
            
            cost = constraint.evaluate(vehicle, route_sequence, **kwargs)
            constraint_name = constraint.get_name()
            
            breakdown[constraint_name] = cost
            total_cost += cost
            
            logger.debug(f"  {constraint_name}: cost={cost:.2f}, hard={constraint.is_hard_constraint()}")
            
            # Check if hard constraint violated
            if constraint.is_hard_constraint() and cost < 0:
                is_feasible = False
                logger.debug(f"  ✗ Hard constraint violated: {constraint_name} for vehicle {vehicle.vehicle_id}, penalty={cost}")
                logger.debug(f"Vehicle {vehicle.vehicle_id} sequence evaluation: total_cost={total_cost:.2f}, feasible={is_feasible}")
                return {
                    'total_cost': total_cost,
                    'breakdown': breakdown,
                    'is_feasible': is_feasible
                }
        
        logger.debug(f"Vehicle {vehicle.vehicle_id} sequence evaluation: total_cost={total_cost:.2f}, feasible={is_feasible}")
        return {
            'total_cost': total_cost,
            'breakdown': breakdown,
            'is_feasible': is_feasible
        }
    
    def get_enabled_constraints(self) -> List[BaseConstraint]:
        """Get list of enabled constraints."""
        return [c for c in self.constraints if c.enabled]
    
    def get_hard_constraints(self) -> List[BaseConstraint]:
        """Get list of hard constraints."""
        return [c for c in self.constraints if c.enabled and c.is_hard_constraint()]
    
    def __repr__(self):
        enabled_count = len(self.get_enabled_constraints())
        hard_count = len(self.get_hard_constraints())
        return f"ConstraintManager(enabled={enabled_count}, hard={hard_count})"
