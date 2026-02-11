"""Shift hours constraint."""
from typing import List
from src.constraints.base import BaseConstraint
from src.models.vehicle import Vehicle
from src.models.route import Route


class ShiftHoursStrictConstraint(BaseConstraint):
    """Enforces maximum working hours for driver compliance."""
    
    def evaluate(self, vehicle: Vehicle, route_sequence: List[Route], **kwargs) -> float:
        """
        Evaluate shift hours constraint.
        
        Args:
            vehicle: Vehicle being evaluated
            route_sequence: Sequence of routes
        
        Returns:
            Penalty if working hours exceed limit, 0 otherwise
        """
        if not self.enabled or not route_sequence:
            return 0.0
        
        max_hours = self.params.get('max_hours', 16)
        calculation_method = self.params.get('calculation_method', 'first_to_last')
        pre_shift_buffer = self.params.get('pre_shift_buffer_hours', 0.5)
        post_shift_buffer = self.params.get('post_shift_buffer_hours', 0.5)
        
        if calculation_method == 'first_to_last':
            # Calculate from first route start to last route end
            first_start = route_sequence[0].plan_start_date_time
            last_end = route_sequence[-1].plan_end_date_time
            total_hours = (last_end - first_start).total_seconds() / 3600.0
        else:  # cumulative
            # Sum of all route durations
            total_hours = sum(route.duration_hours for route in route_sequence)
        
        # Add buffers
        total_hours += pre_shift_buffer + post_shift_buffer
        
        if total_hours > max_hours:
            return self.penalty
        
        return 0.0
    
    def is_hard_constraint(self) -> bool:
        """Shift hours is a hard constraint for regulatory compliance."""
        return True
