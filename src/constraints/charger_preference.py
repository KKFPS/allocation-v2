"""Charger preference constraint."""
import re
from typing import List, Dict, Any, Optional
import json
from src.constraints.base import BaseConstraint
from src.models.vehicle import Vehicle
from src.models.route import Route
from src.utils.logging_config import logger


class ChargerPreferenceConstraint(BaseConstraint):
    """
    Prioritize vehicles on preferred chargers for high-priority routes.
    
    Applies bonus/penalty based on vehicle's current charger location from
    t_vehicle_charge table, considering time windows and global route ordering.
    
    Logic: Routes are ranked by departure time across ALL routes. Vehicles are
    ranked by charger cost (highest first). The r-th leaving route gets assigned
    the vehicle with r-th highest charger cost.
    """
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize charger preference constraint.
        
        Args:
            config: Constraint configuration from MAF containing:
                - enabled: Enable charger-based prioritization
                - params: Dict with:
                    - charger_preference: JSON mapping charger_id -> priority
                    - time_window_start: Apply from this hour (0-23)
                    - time_window_end: Apply until this hour (0-23)
                    - apply_to_position: "first", "all", or "longest"
        """
        super().__init__(config)

        # Parse charger preference map (supports two formats)
        # - New: [87,86]:3,[85,83]:0,[DISC]:2  (list of charger ids per priority)
        # - Legacy: {"87":"3","86":"1","DISC":"-3"}  (single charger id per key)
        charger_map_str = (self.params.get('map') or '{}').strip()

        # Default to disabled if not specified
        self.enabled = self.params.get('enabled', False)

        try:
            if re.match(r'^\s*\[', charger_map_str):
                # New format: [id1,id2,...]:value,[...]:value
                self.charger_preference_map = self._parse_list_format_map(charger_map_str)
            else:
                # Legacy JSON: single key -> value
                parsed = json.loads(charger_map_str)
                self.charger_preference_map = {
                    str(k): float(v) for k, v in parsed.items()
                }
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Invalid charger_preference_map, using empty map: %s", e)
            self.charger_preference_map = {}

        # Parse time window
        self.time_window_start = int(self.params.get('time_window_start', 0))
        self.time_window_end = int(self.params.get('time_window_end', 24))

        # Parse apply_to_position
        self.apply_to_position = self.params.get('apply_to_position', 'first')
        if self.apply_to_position not in ['first', 'all', 'longest']:
            logger.warning("Invalid apply_to_position '%s', using 'first'", self.apply_to_position)
            self.apply_to_position = 'first'

    def _parse_list_format_map(self, s: str) -> Dict[str, float]:
        """Parse [87,86]:3,[85,83]:0,[DISC]:2 into flat charger_id -> cost map."""
        out: Dict[str, float] = {}
        # Match [...]:value (value is optional minus and digits)
        for m in re.finditer(r'\[([^\]]*)\]\s*:\s*(-?\d+)', s):
            key_part, value_str = m.group(1).strip(), m.group(2)
            value = float(value_str)
            # Split key part by comma; each item is an id or DISC
            for part in (p.strip() for p in key_part.split(',')):
                if not part:
                    continue
                if part.upper() == 'DISC':
                    out['DISC'] = value
                else:
                    try:
                        out[str(int(part))] = value
                    except ValueError:
                        out[part] = value
        return out

    def evaluate(self, vehicle: Vehicle, route_sequence: List[Route], **kwargs) -> float:
        """
        Evaluate charger preference for vehicle-route sequence.
        
        Args:
            vehicle: Vehicle being evaluated
            route_sequence: Sequence of routes assigned to vehicle
            **kwargs: Additional context, expects:
                - 'vehicle_charger_map': dict mapping vehicle_id -> charger_id
                - 'all_routes': list of all routes in allocation window
                - 'all_vehicles': list of all vehicles (for ranking)
        
        Returns:
            Bonus/penalty based on charger priority and route position
        """

        # logger.debug(f"Evaluating charger preference enabled: {self.enabled} for vehicle: {vehicle.vehicle_id} with routes: {[r.route_id for r in route_sequence]} and charger map: {kwargs.get('vehicle_charger_map', {})} and charger preference map: {self.charger_preference_map}")
        if not self.enabled:
            return 0.0
        
        if not route_sequence:
            return 0.0
        
        # Check if charger preference map is configured
        if not self.charger_preference_map:
            return 0.0
        
        # Get context
        vehicle_charger_map = kwargs.get('vehicle_charger_map', {})
        all_routes = kwargs.get('all_routes', [])
        all_vehicles = kwargs.get('all_vehicles', [])

        # logger.debug(f"Vehicle charger map: {vehicle_charger_map}")
        # logger.debug(f"All routes: {all_routes}")
        # logger.debug(f"All vehicles: {all_vehicles}")
        
        if not all_routes or not all_vehicles:
            return 0.0
        
        # Get vehicle's charger and its cost
        charger_id = vehicle_charger_map.get(vehicle.vehicle_id)
        charger_key = "DISC" if charger_id is None else str(charger_id)
        vehicle_charger_cost = self.charger_preference_map.get(charger_key, 0)
        
        # If vehicle has no charger cost, no bonus/penalty
        if vehicle_charger_cost == 0:
            return 0.0
        
        # Filter routes in time window and sort by departure time
        routes_in_window = [
            r for r in all_routes 
            if self._is_in_time_window(r.plan_start_date_time.hour)
        ]
        
        if not routes_in_window:
            return 0.0
        
        # Sort routes by departure time (ascending) - global ordering
        routes_in_window_sorted = sorted(
            routes_in_window, 
            key=lambda r: r.plan_start_date_time
        )
        
        # Build route_id -> global_position mapping (0 = first leaving)
        route_position_map = {
            r.route_id: idx 
            for idx, r in enumerate(routes_in_window_sorted)
        }
        
        # Rank all vehicles by charger cost (descending - highest first)
        vehicle_costs = []
        for v in all_vehicles:
            v_charger_id = vehicle_charger_map.get(v.vehicle_id)
            v_charger_key = "DISC" if v_charger_id is None else str(v_charger_id)
            v_cost = self.charger_preference_map.get(v_charger_key, 0)
            vehicle_costs.append((v.vehicle_id, v_cost))
        
        # Sort by cost descending (highest first), stable order for ties
        vehicle_costs_sorted = sorted(vehicle_costs, key=lambda x: -x[1])
        
        # Find vehicle's rank (0-indexed)
        vehicle_rank = None
        for rank, (vid, _) in enumerate(vehicle_costs_sorted):
            if vid == vehicle.vehicle_id:
                vehicle_rank = rank
                break
        
        if vehicle_rank is None:
            return 0.0
        
        # Determine which routes in sequence to evaluate
        target_routes = self._get_target_routes(route_sequence)
        
        total_cost = 0.0
        
        for route in target_routes:
            # Skip if route not in time window
            if not self._is_in_time_window(route.plan_start_date_time.hour):
                continue
            
            # Get route's global position
            route_position = route_position_map.get(route.route_id)
            if route_position is None:
                continue
            
            # Apply cost if vehicle's rank matches route's position
            # r-th leaving route should get vehicle with r-th highest cost
            if route_position == vehicle_rank:
                total_cost += vehicle_charger_cost
        
        return total_cost
    
    def _get_target_routes(self, route_sequence: List[Route]) -> List[Route]:
        """
        Determine which routes to apply charger preference to.
        
        Args:
            route_sequence: Sequence of routes
        
        Returns:
            List of routes to evaluate
        """
        if not route_sequence:
            return []
        
        if self.apply_to_position == 'first':
            return [route_sequence[0]]
        
        elif self.apply_to_position == 'all':
            return route_sequence
        
        elif self.apply_to_position == 'longest':
            # Find the longest route by duration
            longest_route = max(
                route_sequence,
                key=lambda r: (r.plan_end_date_time - r.plan_start_date_time).total_seconds()
            )
            return [longest_route]
        
        return []
    
    def _is_in_time_window(self, hour: int) -> bool:
        """
        Check if hour is within configured time window.
        
        Args:
            hour: Hour of day (0-23)
        
        Returns:
            True if within window
        """
        # Handle window that crosses midnight
        if self.time_window_start <= self.time_window_end:
            return self.time_window_start <= hour < self.time_window_end
        else:
            # Window crosses midnight (e.g., 22:00 to 06:00)
            return hour >= self.time_window_start or hour < self.time_window_end
    
    def is_hard_constraint(self) -> bool:
        """
        Charger preference is a soft constraint (preference, not requirement).
        
        Returns:
            False (soft constraint)
        """
        return False
    
    def __repr__(self):
        return (
            f"ChargerPreferenceConstraint(enabled={self.enabled}, "
            f"time_window={self.time_window_start}-{self.time_window_end}, "
            f"apply_to={self.apply_to_position}, "
            f"chargers={len(self.charger_preference_map)})"
        )
