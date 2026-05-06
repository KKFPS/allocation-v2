"""Cost matrix builder for allocation optimization."""
import csv
import os
from typing import List, Dict, Tuple, Optional
from itertools import combinations
from math import comb

import numpy as np

from src.models.vehicle import Vehicle
from src.models.route import Route
from src.constraints.constraint_manager import ConstraintManager
from src.constraints.turnaround_time import TurnaroundTimeStrictConstraint
from src.config import DEFAULT_TURNAROUND_TIME_MINUTES
from src.utils.logging_config import logger

# Set to True to dump all matrices and constraint params to a single CSV (decision vars left empty).
DEBUG_DUMP_COST_MATRIX_CSV = False
PROGRESS_LOG_EVERY_COMBINATIONS = 5000


def _debug_dump_matrices_to_csv(
    builder: "CostMatrixBuilder",
    sequence_costs: np.ndarray,
    sequences: List,
    metadata: Dict,
    filepath: Optional[str] = None,
) -> None:
    """
    Write a single CSV with all matrices and constraint params for debugging.
    Decision variables (selected 0/1 per sequence) are left empty.
    Only runs when DEBUG_DUMP_COST_MATRIX_CSV is True; call from build_assignment_matrix.
    """
    route_ids = [r.route_id for r in builder.routes]
    n_sequences = len(sequences)
    n_routes = len(route_ids)

    # Build same structures as optimizer: route_coverage, vehicle_to_sequences
    route_coverage = {rid: [] for rid in route_ids}
    vehicle_to_sequences: Dict[int, List[int]] = {}
    for seq_idx, (vehicle_id, route_sequence, _) in enumerate(sequences):
        vehicle_to_sequences.setdefault(vehicle_id, []).append(seq_idx)
        for route in route_sequence:
            if route.route_id in route_coverage:
                route_coverage[route.route_id].append(seq_idx)

    # Route-coverage matrix: row per route, column per sequence (1 if covered, 0 else)
    route_coverage_matrix = []
    for rid in route_ids:
        row = [1 if seq_idx in route_coverage[rid] else 0 for seq_idx in range(n_sequences)]
        route_coverage_matrix.append(row)

    if filepath is None:
        filepath = os.path.join(os.getcwd(), "cost_matrix_debug.csv")

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)

        # --- CONSTRAINT PARAMS ---
        w.writerow(["[CONSTRAINT_PARAMS]"])
        w.writerow(["param", "value"])
        w.writerow(["n_vehicles", builder.n_vehicles])
        w.writerow(["n_routes", builder.n_routes])
        w.writerow(["n_sequences", n_sequences])
        w.writerow(["max_routes_per_vehicle", builder.max_routes_per_vehicle])
        w.writerow(["total_sequences", metadata.get("total_sequences", n_sequences)])
        w.writerow(["feasible_assignments", metadata.get("feasible_assignments", "")])
        w.writerow([])

        # --- SEQUENCE COST MATRIX (one row: header seq_0, seq_1, ... then cost values) ---
        w.writerow(["[COST_MATRIX]"])
        w.writerow(["seq_idx"] + [f"seq_{i}" for i in range(n_sequences)])
        cost_row = ["cost"] + [float(sequence_costs[i]) for i in range(n_sequences)]
        w.writerow(cost_row)
        w.writerow([])

        # --- SEQUENCE DETAILS: seq_idx, vehicle_id, route_ids, cost, selected (empty) ---
        w.writerow(["[SEQUENCE_DETAILS]"])
        w.writerow(["seq_idx", "vehicle_id", "route_ids", "cost", "selected"])
        for i in range(n_sequences):
            vehicle_id, route_sequence, cost = sequences[i]
            route_ids_str = ";".join(r.route_id for r in route_sequence)
            w.writerow([i, vehicle_id, route_ids_str, float(cost), ""])
        w.writerow([])

        # --- ROUTE COVERAGE MATRIX (routes x sequences) ---
        w.writerow(["[ROUTE_COVERAGE_MATRIX]"])
        w.writerow(["route_id"] + [f"seq_{i}" for i in range(n_sequences)])
        for r_idx, rid in enumerate(route_ids):
            w.writerow([rid] + route_coverage_matrix[r_idx])
        w.writerow([])

        # --- VEHICLE -> SEQUENCE INDICES ---
        w.writerow(["[VEHICLE_TO_SEQUENCES]"])
        w.writerow(["vehicle_id", "seq_indices"])
        for vehicle_id in sorted(vehicle_to_sequences.keys()):
            indices_str = ";".join(str(s) for s in vehicle_to_sequences[vehicle_id])
            w.writerow([vehicle_id, indices_str])

    logger.info(f"Debug CSV written: {filepath}")


class CostMatrixBuilder:
    """Builds cost matrix for vehicle-route assignments."""
    
    def __init__(self, vehicles: List[Vehicle], routes: List[Route], 
                 constraint_manager: ConstraintManager, max_routes_per_vehicle: int = 5,
                 vehicle_charger_map: Dict[int, Optional[str]] = None):
        """
        Initialize cost matrix builder.
        
        Args:
            vehicles: List of available vehicles
            routes: List of routes to allocate
            constraint_manager: Constraint evaluation manager
            max_routes_per_vehicle: Maximum routes per vehicle in window
            vehicle_charger_map: Dict mapping vehicle_id -> charger_id or None (one vehicle per charger)
        """
        self.vehicles = vehicles
        self.routes = routes
        self.constraint_manager = constraint_manager
        self.max_routes_per_vehicle = max_routes_per_vehicle
        self.vehicle_charger_map = vehicle_charger_map or {}
        
        self.n_vehicles = len(vehicles)
        self.n_routes = len(routes)
        
        # Sort routes by start time for sequencing
        self.routes.sort(key=lambda r: r.plan_start_date_time)
        self.turnaround_minutes = self._resolve_overlap_turnaround_minutes()
        self.route_pair_compatibility = self._build_route_pair_compatibility()
    
    def _resolve_overlap_turnaround_minutes(self) -> int:
        """
        Resolve turnaround minutes used for overlap pruning.
        
        Uses strict-turnaround constraint parameter when available to keep
        overlap pruning aligned with active constraint configuration.
        """
        for constraint in self.constraint_manager.get_enabled_constraints():
            if isinstance(constraint, TurnaroundTimeStrictConstraint):
                minimum = constraint.params.get(
                    'minimum_minutes',
                    DEFAULT_TURNAROUND_TIME_MINUTES
                )
                try:
                    return int(minimum)
                except (TypeError, ValueError):
                    return DEFAULT_TURNAROUND_TIME_MINUTES
        
        return DEFAULT_TURNAROUND_TIME_MINUTES
    
    def _build_route_pair_compatibility(self) -> Dict[Tuple[str, str], bool]:
        """Precompute compatibility for all route pairs."""
        compatibility: Dict[Tuple[str, str], bool] = {}
        
        for i in range(len(self.routes) - 1):
            left = self.routes[i]
            for j in range(i + 1, len(self.routes)):
                right = self.routes[j]
                key = tuple(sorted((left.route_id, right.route_id)))
                compatibility[key] = not left.overlaps_with(
                    right, self.turnaround_minutes
                )
        
        return compatibility
    
    def _is_non_overlapping_sequence(self, route_sequence: List[Route]) -> bool:
        """Check sequence overlap feasibility using cached pair compatibility."""
        if len(route_sequence) < 2:
            return True
        
        # Sequence is sorted by start time; adjacent checks are sufficient.
        for idx in range(len(route_sequence) - 1):
            left = route_sequence[idx]
            right = route_sequence[idx + 1]
            key = tuple(sorted((left.route_id, right.route_id)))
            
            if not self.route_pair_compatibility.get(
                key,
                not left.overlaps_with(right, self.turnaround_minutes)
            ):
                return False
        
        return True
    
    def _estimate_max_routes_to_combine(self) -> int:
        """
        Estimate practical upper bound for sequence length from route timing.
        
        Uses:
        - average route duration
        - earliest route start and latest route end in the current window
        Then adds +1 as an edge-case buffer.
        """
        if not self.routes:
            return 1
        
        earliest_start = min(r.plan_start_date_time for r in self.routes)
        latest_end = max(r.plan_end_date_time for r in self.routes)
        window_minutes = max(
            1.0,
            (latest_end - earliest_start).total_seconds() / 60.0
        )
        
        avg_route_minutes = sum(r.duration_minutes for r in self.routes) / len(self.routes)
        if avg_route_minutes <= 0:
            return min(self.max_routes_per_vehicle, self.n_routes)
        
        estimated_max = int(window_minutes // avg_route_minutes) + 1
        estimated_max = max(1, estimated_max)
        
        return min(self.max_routes_per_vehicle, self.n_routes, estimated_max)
    
    def generate_feasible_sequences(self, vehicle: Vehicle) -> List[Tuple[List[Route], float]]:
        """
        Generate all feasible route sequences for a vehicle.
        
        Args:
            vehicle: Vehicle to generate sequences for
        
        Returns:
            List of (route_sequence, cost) tuples
        """
        logger.debug(f"\nGenerating feasible sequences for vehicle {vehicle.vehicle_id} (label: {vehicle.telematic_label})")
        logger.debug(f"  Available energy: {vehicle.available_energy_kwh:.1f} kWh, Available from: {vehicle.available_time}")
        
        feasible_sequences = []
        single_route_feasible = 0
        
        # Single route assignments
        for route in self.routes:
            evaluation = self.constraint_manager.evaluate_sequence(
                vehicle, [route], 
                vehicle_charger_map=self.vehicle_charger_map,
                all_routes=self.routes,
                all_vehicles=self.vehicles
            )
            if evaluation is None:
                logger.warning(
                    f"evaluate_sequence returned None for vehicle {vehicle.vehicle_id} route {route.route_id}; skipping"
                )
                continue
            if evaluation['is_feasible']:
                feasible_sequences.append(([route], evaluation['total_cost']))
                single_route_feasible += 1
        
        logger.debug(f"  Single routes: {single_route_feasible}/{len(self.routes)} feasible")
        
        # Multi-route sequences (up to max_routes_per_vehicle)
        multi_route_feasible = 0
        max_routes_to_combine = self._estimate_max_routes_to_combine()
        logger.debug(
            f"  Max routes to combine: {max_routes_to_combine} "
            f"(config={self.max_routes_per_vehicle}, routes={self.n_routes})"
        )
        
        for seq_length in range(2, max_routes_to_combine + 1):
            seq_length_feasible = 0
            seq_length_processed = 0
            total_combos_for_length = comb(self.n_routes, seq_length)
            logger.debug(
                f"  Starting sequences of length {seq_length}: "
                f"{total_combos_for_length} combinations to evaluate"
            )
            # Generate combinations of routes
            for route_combo in combinations(self.routes, seq_length):
                seq_length_processed += 1
                if (
                    seq_length >= 3 and
                    seq_length_processed % PROGRESS_LOG_EVERY_COMBINATIONS == 0
                ):
                    logger.debug(
                        f"    Progress length {seq_length}: "
                        f"{seq_length_processed}/{total_combos_for_length} processed, "
                        f"{seq_length_feasible} feasible so far"
                    )
                # combinations(self.routes, ...) preserves order from self.routes.
                route_sequence = list(route_combo)
                
                # Early prune overlap-infeasible sequences before full evaluation.
                if not self._is_non_overlapping_sequence(route_sequence):
                    continue
                
                # Evaluate sequence
                evaluation = self.constraint_manager.evaluate_sequence(
                    vehicle, route_sequence, 
                    vehicle_charger_map=self.vehicle_charger_map,
                    all_routes=self.routes,
                    all_vehicles=self.vehicles
                )
                if evaluation is None:
                    logger.warning(
                        f"evaluate_sequence returned None for vehicle {vehicle.vehicle_id} "
                        f"sequence {[r.route_id for r in route_sequence]}; skipping"
                    )
                    continue
                if evaluation['is_feasible']:
                    feasible_sequences.append((route_sequence, evaluation['total_cost']))
                    seq_length_feasible += 1
                    multi_route_feasible += 1
            
            if seq_length_feasible > 0:
                logger.debug(f"  Sequences of length {seq_length}: {seq_length_feasible} feasible")
            else:
                logger.debug(f"  Sequences of length {seq_length}: 0 feasible")
        
        logger.debug(f"  Multi-route sequences: {multi_route_feasible} total feasible")
        logger.debug(f"Vehicle {vehicle.vehicle_id}: {len(feasible_sequences)} total feasible sequences\n")
        return feasible_sequences
    
    def build_assignment_matrix(self) -> Tuple[np.ndarray, List, Dict]:
        """
        Build assignment cost matrix.
        
        Returns:
            Tuple of (cost_matrix, sequences, metadata)
            - cost_matrix: 2D numpy array (n_vehicles x n_sequences)
            - sequences: List of (vehicle_id, route_sequence, cost) tuples
            - metadata: Dictionary with matrix statistics
        """
        logger.debug(f"\n{'='*60}")
        logger.debug(f"BUILDING COST MATRIX")
        logger.debug(f"{'='*60}")
        logger.debug(f"Vehicles: {self.n_vehicles}, Routes: {self.n_routes}, Max routes per vehicle: {self.max_routes_per_vehicle}")
        
        all_sequences = []
        sequence_costs = []
        
        # Generate feasible sequences for each vehicle
        for vehicle in self.vehicles:
            vehicle_sequences = self.generate_feasible_sequences(vehicle)
            
            for route_sequence, cost in vehicle_sequences:
                all_sequences.append((vehicle.vehicle_id, route_sequence, cost))
                sequence_costs.append(cost)
        
        logger.info(f"Generated {len(all_sequences)} total feasible sequences across {self.n_vehicles} vehicles")
        logger.debug(f"\nCost statistics:")
        logger.debug(f"  Min cost: {min(sequence_costs) if sequence_costs else 'N/A'}")
        logger.debug(f"  Max cost: {max(sequence_costs) if sequence_costs else 'N/A'}")
        logger.debug(f"  Avg cost: {f'{sum(sequence_costs)/len(sequence_costs):.2f}' if sequence_costs else 'N/A'}")
        
        # Build cost matrix (we'll use negative costs since Hexaly maximizes)
        # Each column represents a sequence (vehicle + routes)
        # We need to ensure each route is assigned exactly once
        
        metadata = {
            'total_sequences': len(all_sequences),
            'vehicles': self.n_vehicles,
            'routes': self.n_routes,
            'max_routes_per_vehicle': self.max_routes_per_vehicle,
            'feasible_assignments': len([c for c in sequence_costs if c >= 0])
        }
        
        logger.debug(f"\nMatrix metadata:")
        logger.debug(f"  Total sequences: {metadata['total_sequences']}")
        logger.debug(f"  Feasible assignments (cost >= 0): {metadata['feasible_assignments']}")

        if metadata['feasible_assignments'] == 0:
            logger.warning("No feasible assignments found")
            return np.array([]), [], metadata

        logger.debug(f"  Coverage: {metadata['feasible_assignments']/metadata['total_sequences']*100:.1f}% feasible")
        logger.debug(f"{'='*60}\n")

        if DEBUG_DUMP_COST_MATRIX_CSV:
            _debug_dump_matrices_to_csv(
                self, np.array(sequence_costs), all_sequences, metadata
            )

        return np.array(sequence_costs), all_sequences, metadata
    
    def get_route_sequence_map(self, sequences: List) -> Dict[int, List[str]]:
        """
        Create mapping of sequence indices to route IDs.
        
        Args:
            sequences: List of sequence tuples
        
        Returns:
            Dictionary mapping sequence_idx to list of route_ids
        """
        sequence_map = {}
        
        for idx, (vehicle_id, route_sequence, cost) in enumerate(sequences):
            sequence_map[idx] = [route.route_id for route in route_sequence]
        
        return sequence_map
