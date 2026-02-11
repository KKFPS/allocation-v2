"""Base constraint class."""
from abc import ABC, abstractmethod
from typing import List, Dict, Any
from src.models.vehicle import Vehicle
from src.models.route import Route


class BaseConstraint(ABC):
    """Abstract base class for allocation constraints."""
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize constraint with configuration.
        
        Args:
            config: Constraint configuration from MAF
        """
        self.enabled = config.get('enabled', True)
        self.params = config.get('params', {})
        self.penalty = config.get('penalty', -20)
    
    @abstractmethod
    def evaluate(self, vehicle: Vehicle, route_sequence: List[Route], **kwargs) -> float:
        """
        Evaluate constraint for a vehicle-route sequence.
        
        Args:
            vehicle: Vehicle being evaluated
            route_sequence: Sequence of routes assigned to vehicle
            **kwargs: Additional context
        
        Returns:
            Cost/penalty (negative = penalty, positive = bonus, 0 = neutral)
        """
        pass
    
    @abstractmethod
    def is_hard_constraint(self) -> bool:
        """
        Indicate if this is a hard constraint (must not be violated).
        
        Returns:
            True if hard constraint, False if soft
        """
        pass
    
    def get_name(self) -> str:
        """Get constraint name."""
        return self.__class__.__name__
    
    def __repr__(self):
        return f"{self.get_name()}(enabled={self.enabled}, penalty={self.penalty})"
