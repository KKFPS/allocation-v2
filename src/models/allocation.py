"""Allocation data model."""
from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime


@dataclass
class RouteAllocation:
    """Represents a single route-to-vehicle allocation."""
    
    route_id: str
    vehicle_id: int
    estimated_arrival: datetime
    estimated_arrival_soc: float
    cost: float = 0.0
    
    def __repr__(self):
        return f"RouteAllocation(route={self.route_id}, vehicle={self.vehicle_id}, soc={self.estimated_arrival_soc:.1f}%)"


@dataclass
class VehicleRouteSequence:
    """Represents a sequence of routes assigned to a vehicle."""
    
    vehicle_id: int
    routes: List[str] = field(default_factory=list)
    total_cost: float = 0.0
    total_working_hours: float = 0.0
    final_soc: float = 100.0
    
    def add_route(self, route_id: str, cost: float = 0.0):
        """Add a route to the sequence."""
        self.routes.append(route_id)
        self.total_cost += cost
    
    def __repr__(self):
        return f"VehicleSequence(vehicle={self.vehicle_id}, routes={len(self.routes)}, cost={self.total_cost:.2f})"


@dataclass
class AllocationResult:
    """Complete allocation result for a site."""
    
    allocation_id: int
    site_id: int
    run_datetime: datetime
    window_start: datetime
    window_end: datetime
    allocations: List[RouteAllocation] = field(default_factory=list)
    unallocated_routes: List[str] = field(default_factory=list)
    total_score: float = 0.0
    routes_in_window: int = 0
    routes_allocated: int = 0
    routes_overlapping_count: int = 0
    status: str = 'N'
    
    def add_allocation(self, allocation: RouteAllocation):
        """Add a route allocation."""
        self.allocations.append(allocation)
        self.routes_allocated += 1
        self.total_score += allocation.cost
    
    def mark_unallocated(self, route_id: str):
        """Mark a route as unallocated."""
        self.unallocated_routes.append(route_id)
    
    def is_acceptable(self, min_score: float = -4.0) -> bool:
        """
        Check if allocation quality is acceptable.
        
        Args:
            min_score: Minimum acceptable score
        
        Returns:
            True if acceptable, False otherwise
        """
        return self.total_score >= min_score
    
    def get_vehicle_sequences(self) -> dict:
        """
        Get routes grouped by vehicle.
        
        Returns:
            Dictionary mapping vehicle_id to list of route_ids
        """
        sequences = {}
        for alloc in self.allocations:
            if alloc.vehicle_id not in sequences:
                sequences[alloc.vehicle_id] = []
            sequences[alloc.vehicle_id].append(alloc.route_id)
        return sequences
    
    def __repr__(self):
        return f"AllocationResult(id={self.allocation_id}, allocated={self.routes_allocated}/{self.routes_in_window}, score={self.total_score:.2f})"
