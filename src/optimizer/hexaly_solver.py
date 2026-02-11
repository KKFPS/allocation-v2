"""Hexaly optimization solver integration."""
import hexaly.optimizer as hx
from typing import List, Dict, Tuple
import numpy as np
from src.models.allocation import RouteAllocation, AllocationResult
from src.utils.logging_config import logger
from src.config import IS_HEXALY_ACTIVE


# Weight so that one extra allocated route dominates any realistic score delta
ROUTE_COUNT_WEIGHT = 1e2


class HexalySolver:
    """
    Hexaly Cloud optimizer for vehicle-route allocation.
    
    Maximizes the number of routes allocated (then total score). Each route is
    covered at most once; each vehicle is used by at most one sequence. Some
    routes may be left unallocated when mathematically justified.
    """
    
    def __init__(self, time_limit_seconds: int = 30):
        """
        Initialize Hexaly solver.
        
        Args:
            time_limit_seconds: Maximum solve time
        """
        self.time_limit = time_limit_seconds
    
    def solve(self, sequences: List[Tuple], route_ids: List[str], 
              sequence_costs: np.ndarray) -> Dict:
        """
        Solve allocation optimization problem.
        
        Maximizes number of routes allocated, then total score. Each vehicle
        is used by at most one sequence; some routes may be left unallocated.
        
        Args:
            sequences: List of (vehicle_id, route_sequence, cost) tuples
            route_ids: List of all route IDs that need assignment
            sequence_costs: Costs for each sequence
        
        Returns:
            Dictionary with selected_sequences, total_score, solve_time, status
        """
        # Check if Hexaly is active, otherwise use greedy fallback
        if not IS_HEXALY_ACTIVE:
            logger.warning("Hexaly not active - using greedy fallback")
            return self._greedy_fallback(sequences, route_ids, sequence_costs)
        
        try:
            with hx.HexalyOptimizer() as optimizer:
                model = optimizer.model
                
                n_sequences = len(sequences)
                n_routes = len(route_ids)
                
                logger.info(f"Starting Hexaly optimization: {n_sequences} sequences, {n_routes} routes")
                
                # Decision variables: binary selection for each sequence
                sequence_vars = [model.bool() for _ in range(n_sequences)]
                
                # Build route-to-sequence mapping
                route_coverage = {route_id: [] for route_id in route_ids}
                # Build vehicle-to-sequence mapping (one sequence per vehicle)
                vehicle_to_sequences = {}
                for seq_idx, (vehicle_id, route_sequence, cost) in enumerate(sequences):
                    vehicle_to_sequences.setdefault(vehicle_id, []).append(seq_idx)
                    for route in route_sequence:
                        if route.route_id in route_coverage:
                            route_coverage[route.route_id].append(seq_idx)
                
                # Constraint: Each vehicle used by at most one sequence
                for vehicle_id, seq_indices in vehicle_to_sequences.items():
                    model.constraint(
                        model.sum([sequence_vars[i] for i in seq_indices]) <= 1
                    )
                
                # Constraints: Each route covered at most once; route_covered for objective
                route_covered_vars = {}
                uncovered_routes = 0
                for route_id in route_ids:
                    covering_sequences = route_coverage[route_id]
                    
                    if covering_sequences:
                        coverage_sum = model.sum([sequence_vars[idx] for idx in covering_sequences])
                        # At most one selected sequence covers this route
                        model.constraint(coverage_sum <= 1)
                        # Binary: route is covered iff at least one covering sequence selected
                        route_covered = model.bool()
                        route_covered_vars[route_id] = route_covered
                        model.constraint(route_covered <= coverage_sum)
                        model.constraint(coverage_sum <= len(covering_sequences) * route_covered)
                        logger.debug(f"Route {route_id}: {len(covering_sequences)} covering sequences")
                    else:
                        uncovered_routes += 1
                        logger.debug(f"Route {route_id}: NO covering sequences (cannot be allocated)")
                
                if uncovered_routes > 0:
                    logger.warning(f"{uncovered_routes} routes have no feasible assignments")
                
                # Objective: Maximize routes allocated, then total score
                score_term = model.sum([sequence_vars[i] * sequence_costs[i] for i in range(n_sequences)])
                if route_covered_vars:
                    route_count_term = model.sum(list(route_covered_vars.values()))
                    objective = ROUTE_COUNT_WEIGHT * route_count_term + score_term
                else:
                    objective = score_term
                model.maximize(objective)
                
                model.close()
                
                # Solve
                optimizer.param.time_limit = self.time_limit
                optimizer.solve()
                
                # Extract solution
                selected_indices = [i for i in range(n_sequences) if sequence_vars[i].value == 1]
                selected_sequences = [sequences[i] for i in selected_indices]
                total_score = sum(sequences[i][2] for i in selected_indices)
                routes_allocated = sum(1 for r in route_covered_vars if route_covered_vars[r].value == 1)
                routes_unallocated = n_routes - routes_allocated
                
                logger.info(
                    f"Optimization complete: {len(selected_sequences)} sequences selected, "
                    f"{routes_allocated}/{n_routes} routes allocated, score={total_score:.2f}"
                )
                if routes_unallocated > 0:
                    logger.info(f"  {routes_unallocated} routes left unallocated")
                logger.debug(f"\nSelected sequences:")
                for idx in selected_indices:
                    vehicle_id, route_seq, cost = sequences[idx]
                    seq_route_ids = [r.route_id for r in route_seq]
                    logger.debug(f"  Vehicle {vehicle_id}: {len(route_seq)} routes {seq_route_ids}, cost={cost:.2f}")
                
                return {
                    'selected_sequences': selected_sequences,
                    'total_score': total_score,
                    'solve_time': optimizer.statistics.get_running_time(),
                    'status': 'optimal' if optimizer.solution.get_status() == hx.HxSolutionStatus.OPTIMAL else 'feasible'
                }
        
        except Exception as e:
            logger.error(f"Hexaly solver failed: {e}")
            # Fall back to greedy heuristic
            return self._greedy_fallback(sequences, route_ids, sequence_costs)
    
    def _greedy_fallback(self, sequences: List[Tuple], route_ids: List[str], 
                        sequence_costs: np.ndarray) -> Dict:
        """
        Greedy heuristic fallback if Hexaly fails.
        
        Args:
            sequences: List of sequences
            route_ids: Route IDs to cover
            sequence_costs: Sequence costs
        
        Returns:
            Dictionary with selected sequences
        """
        logger.warning("Using greedy fallback solver")
        
        # Sort sequences by cost (best first)
        sorted_indices = np.argsort(sequence_costs)[::-1]
        
        selected_sequences = []
        covered_routes = set()
        used_vehicles = set()
        total_score = 0.0
        
        for idx in sorted_indices:
            vehicle_id, route_sequence, cost = sequences[idx]
            route_ids_in_seq = [r.route_id for r in route_sequence]
            
            # Each vehicle can be used by at most one sequence
            if vehicle_id in used_vehicles:
                continue
            # Check if any routes already covered
            if any(rid in covered_routes for rid in route_ids_in_seq):
                continue
            
            # Select this sequence
            selected_sequences.append(sequences[idx])
            covered_routes.update(route_ids_in_seq)
            used_vehicles.add(vehicle_id)
            total_score += cost
            
            # If all routes covered, done
            if len(covered_routes) == len(route_ids):
                break
        
        logger.info(
            f"Greedy solution: {len(selected_sequences)} sequences, "
            f"{len(covered_routes)}/{len(route_ids)} routes allocated, score={total_score:.2f}"
        )
        
        return {
            'selected_sequences': selected_sequences,
            'total_score': total_score,
            'solve_time': 0,
            'status': 'greedy'
        }
    
    def create_allocation_result(self, solution: Dict, allocation_id: int, 
                                site_id: int, window_start, window_end, 
                                all_route_ids: List[str]) -> AllocationResult:
        """
        Convert solver solution to AllocationResult object.
        
        Args:
            solution: Solver solution dictionary
            allocation_id: Allocation run ID
            site_id: Site ID
            window_start: Window start time
            window_end: Window end time
            all_route_ids: All route IDs in window
        
        Returns:
            AllocationResult object
        """
        from datetime import datetime
        
        result = AllocationResult(
            allocation_id=allocation_id,
            site_id=site_id,
            run_datetime=datetime.now(),
            window_start=window_start,
            window_end=window_end,
            total_score=solution['total_score'],
            routes_in_window=len(all_route_ids),
            status='P'
        )
        
        allocated_routes = set()
        
        # Process selected sequences
        for vehicle_id, route_sequence, cost in solution['selected_sequences']:
            for route in route_sequence:
                # Calculate estimated arrival and SOC
                # (simplified - should use actual vehicle state)
                allocation = RouteAllocation(
                    route_id=route.route_id,
                    vehicle_id=vehicle_id,
                    estimated_arrival=route.plan_end_date_time,
                    estimated_arrival_soc=80.0,  # Placeholder
                    cost=cost / len(route_sequence)  # Distribute cost
                )
                
                result.add_allocation(allocation)
                allocated_routes.add(route.route_id)
        
        # Mark unallocated routes
        for route_id in all_route_ids:
            if route_id not in allocated_routes:
                result.mark_unallocated(route_id)
        
        return result
