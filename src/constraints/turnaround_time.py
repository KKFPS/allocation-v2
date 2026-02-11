"""Turnaround time constraints."""
from typing import List
from datetime import timedelta
from src.constraints.base import BaseConstraint
from src.models.vehicle import Vehicle
from src.models.route import Route


class TurnaroundTimeStrictConstraint(BaseConstraint):
    """Enforces minimum turnaround time between sequential routes."""
    
    def evaluate(self, vehicle: Vehicle, route_sequence: List[Route], **kwargs) -> float:
        """
        Evaluate strict turnaround time constraint.
        
        Args:
            vehicle: Vehicle being evaluated
            route_sequence: Sequence of routes
        
        Returns:
            Penalty if turnaround too short, 0 otherwise
        """
        if not self.enabled or len(route_sequence) < 2:
            return 0.0
        
        minimum_minutes = self.params.get('minimum_minutes', 45)
        minimum_delta = timedelta(minutes=minimum_minutes)
        
        # Check turnaround between each consecutive route pair
        for i in range(len(route_sequence) - 1):
            current_route = route_sequence[i]
            next_route = route_sequence[i + 1]
            
            turnaround = next_route.plan_start_date_time - current_route.plan_end_date_time
            
            if turnaround < minimum_delta:
                return self.penalty
        
        return 0.0
    
    def is_hard_constraint(self) -> bool:
        """Strict turnaround is a hard constraint."""
        return True


class TurnaroundTimePreferredConstraint(BaseConstraint):
    """Soft constraint encouraging comfortable turnaround times."""
    
    def evaluate(self, vehicle: Vehicle, route_sequence: List[Route], **kwargs) -> float:
        """
        Evaluate preferred turnaround time constraint.
        
        Args:
            vehicle: Vehicle being evaluated
            route_sequence: Sequence of routes
        
        Returns:
            Small penalty if turnaround below preferred thresholds
        """
        if not self.enabled or len(route_sequence) < 2:
            return 0.0
        
        standard_minutes = self.params.get('standard_minutes', 75)
        optimal_minutes = self.params.get('optimal_minutes', 90)
        penalty_standard = self.params.get('penalty_standard', -2)
        penalty_optimal = self.params.get('penalty_optimal', -1)
        
        total_penalty = 0.0
        
        for i in range(len(route_sequence) - 1):
            current_route = route_sequence[i]
            next_route = route_sequence[i + 1]
            
            turnaround_minutes = (next_route.plan_start_date_time - current_route.plan_end_date_time).total_seconds() / 60.0
            
            if turnaround_minutes < standard_minutes:
                total_penalty += penalty_standard
            elif turnaround_minutes < optimal_minutes:
                total_penalty += penalty_optimal
        
        return total_penalty
    
    def is_hard_constraint(self) -> bool:
        """Preferred turnaround is a soft constraint."""
        return False
