"""Route overlap prevention constraint."""
from typing import List
from src.constraints.base import BaseConstraint
from src.models.vehicle import Vehicle
from src.models.route import Route


class RouteOverlapConstraint(BaseConstraint):
    """
    Prevents temporal overlaps in route sequences.
    
    This is a mandatory constraint - a vehicle cannot be in two places at once.
    """
    
    def evaluate(self, vehicle: Vehicle, route_sequence: List[Route], **kwargs) -> float:
        """
        Evaluate route overlap constraint.
        
        Args:
            vehicle: Vehicle being evaluated
            route_sequence: Sequence of routes
        
        Returns:
            Large penalty if routes overlap, 0 otherwise
        """
        if len(route_sequence) < 2:
            return 0.0
        
        # This constraint is always enabled
        turnaround_minutes = kwargs.get('turnaround_minutes', 0)
        
        # Check all route pairs for overlap
        for i in range(len(route_sequence) - 1):
            for j in range(i + 1, len(route_sequence)):
                if route_sequence[i].overlaps_with(route_sequence[j], turnaround_minutes):
                    # Routes overlap - this is physically impossible
                    return self.penalty
        
        return 0.0
    
    def is_hard_constraint(self) -> bool:
        """Route overlap is a mandatory hard constraint."""
        return True
