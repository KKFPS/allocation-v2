"""Hexaly optimization solver for charge scheduling."""
import hexaly.optimizer as hx
from typing import List, Dict, Tuple, Optional
from datetime import datetime

from src.models.scheduler import (
    VehicleChargeState, RouteEnergyRequirement, VehicleAvailability,
    ChargeSlot, VehicleChargeSchedule
)
from src.models.vehicle import Vehicle
from src.utils.logging_config import logger
from src.config import IS_HEXALY_ACTIVE, DEFAULT_MIN_SOC_PERCENT


class ChargeOptimizer:
    """
    Hexaly optimizer for vehicle charge scheduling.
    
    Minimizes total charging cost while satisfying:
    - Route energy requirements at departure times
    - Site capacity constraints
    - Vehicle availability windows
    - Battery SOC bounds
    - Charging rate limits
    """
    
    def __init__(self, time_limit_seconds: int = 300):
        """
        Initialize charge optimizer.
        
        Args:
            time_limit_seconds: Maximum solve time
        """
        self.time_limit = time_limit_seconds
    
    def optimize(
        self,
        schedule_id: int,
        vehicles: List[Vehicle],
        vehicle_states: Dict[int, VehicleChargeState],
        energy_requirements: Dict[int, List[RouteEnergyRequirement]],
        availability_matrices: Dict[int, VehicleAvailability],
        time_slots: List[datetime],
        forecast_data: Dict[datetime, float],
        price_data: Dict[datetime, Tuple[float, bool]],
        site_capacity_kw: float,
        target_soc_percent: float,
        triad_penalty_factor: float,
        synthetic_time_price_factor: float,
        min_soc_percent: Optional[float] = None
    ) -> Dict:
        """
        Optimize charging schedule for all vehicles.

        Args:
            schedule_id: Schedule identifier
            vehicles: List of vehicles to schedule
            vehicle_states: Current state of each vehicle
            energy_requirements: Route energy requirements per vehicle
            availability_matrices: Time-slotted availability per vehicle
            time_slots: List of 30-minute time slots
            forecast_data: Site energy forecast (kW) per time slot
            price_data: (price, is_triad) per time slot
            site_capacity_kw: Available site capacity (kW)
            target_soc_percent: Default target SOC for vehicles without routes
            triad_penalty_factor: TRIAD period penalty multiplier
            synthetic_time_price_factor: Time preference factor
            min_soc_percent: Minimum SOC (%) to charge to (default from config, e.g. 75%)

        Returns:
            Dictionary with vehicle_schedules, total_cost, solve_time, status
        """
        if min_soc_percent is None:
            min_soc_percent = DEFAULT_MIN_SOC_PERCENT
        if not IS_HEXALY_ACTIVE:
            logger.warning("Hexaly not active - using greedy fallback")
            return self._greedy_fallback(
                schedule_id, vehicles, vehicle_states, energy_requirements,
                availability_matrices, time_slots, price_data, target_soc_percent,
                min_soc_percent
            )
        
        try:

            for vehicle in vehicles:
                state = vehicle_states.get(vehicle.vehicle_id)
                requirements = energy_requirements.get(vehicle.vehicle_id, [])

                for requirement in requirements:
                    checkpoint_idx = self._find_time_slot_index(
                        requirement.plan_start_date_time, time_slots
                    )

                    if checkpoint_idx is None:
                        continue

                    required_energy = max(
                        0.0,
                        requirement.cumulative_energy_kwh - state.current_soc_kwh
                    )

                    max_possible = 0.0
                    for t in range(checkpoint_idx):
                        if availability_matrices[vehicle.vehicle_id].availability_matrix[t]:
                            max_possible += state.ac_charge_rate_kw * 0.5

                    if max_possible < required_energy:
                        logger.error(
                            f"Vehicle {vehicle.vehicle_id} infeasible: "
                            f"required={required_energy:.2f}, "
                            f"max_possible={max_possible:.2f} "
                            f"plan_start_date_time={requirement.plan_start_date_time} "
                            f"checkpoint_idx={checkpoint_idx} "
                            # f"availability_matrix={availability_matrices[vehicle.vehicle_id].availability_matrix} "
                        )

            with hx.HexalyOptimizer() as optimizer:
                model = optimizer.model
                
                n_slots = len(time_slots)
                n_vehicles = len(vehicles)
                
                logger.info(f"Building Hexaly charge optimization model: "
                           f"{n_vehicles} vehicles, {n_slots} time slots")
                
                # Create vehicle index mapping
                vehicle_to_idx = {v.vehicle_id: idx for idx, v in enumerate(vehicles)}

                # ----- Debug: log requirements and constraints before optimization -----
                logger.info(
                    "[OPTIMIZER INPUT] schedule_id=%s | vehicles=%s | time_slots=%s | "
                    "site_capacity_kw=%.2f | target_soc_percent=%.1f | min_soc_percent=%.1f | "
                    "triad_penalty=%.2f | time_price_factor=%.4f",
                    schedule_id, n_vehicles, n_slots,
                    site_capacity_kw, target_soc_percent, min_soc_percent,
                    triad_penalty_factor, synthetic_time_price_factor,
                )
                if time_slots:
                    logger.info(
                        "[OPTIMIZER INPUT] Time window: %s -> %s",
                        time_slots[0].isoformat(), time_slots[-1].isoformat(),
                    )
                # Forecast summary
                if forecast_data:
                    fvals = list(forecast_data.values())
                    logger.info(
                        "[OPTIMIZER INPUT] Forecast: slots=%s | min=%.2f kW | max=%.2f kW | mean=%.2f kW",
                        len(forecast_data), min(fvals), max(fvals), sum(fvals) / len(fvals),
                    )
                # Price summary
                if price_data:
                    prices = [p[0] for p in price_data.values()]
                    triad_count = sum(1 for p in price_data.values() if p[1])
                    logger.info(
                        "[OPTIMIZER INPUT] Price: slots=%s | min=%.4f | max=%.4f | TRIAD slots=%s",
                        len(price_data), min(prices), max(prices), triad_count,
                    )
                # Per-vehicle requirements and constraints
                for vehicle in vehicles:
                    state = vehicle_states.get(vehicle.vehicle_id)
                    requirements = energy_requirements.get(vehicle.vehicle_id, [])
                    availability = availability_matrices.get(vehicle.vehicle_id)
                    avail_slots = (
                        sum(1 for x in availability.availability_matrix if x)
                        if availability and getattr(availability, "availability_matrix", None)
                        else 0
                    )
                    if state:
                        logger.info(
                            "[OPTIMIZER INPUT] Vehicle %s: soc=%.2f kWh (%.1f%%) | capacity=%.2f kWh | "
                            "charge_rate=%.2f kW | available_slots=%s/%s",
                            vehicle.vehicle_id,
                            state.current_soc_kwh,
                            state.current_soc_percent,
                            state.battery_capacity_kwh,
                            state.ac_charge_rate_kw,
                            avail_slots,
                            n_slots,
                        )
                    for req in requirements:
                        needed = max(0.0, req.cumulative_energy_kwh - (state.current_soc_kwh if state else 0))
                        logger.info(
                            "[OPTIMIZER INPUT]   Route req: route_id=%s | departure=%s | "
                            "cumulative_energy_kwh=%.2f | needed_from_charge=%.2f kWh",
                            req.route_id,
                            req.plan_start_date_time.isoformat(),
                            req.cumulative_energy_kwh,
                            needed,
                        )
                    if state and not requirements:
                        target_kwh = (target_soc_percent / 100.0) * state.battery_capacity_kwh
                        needed = max(0.0, target_kwh - state.current_soc_kwh)
                        logger.info(
                            "[OPTIMIZER INPUT]   No routes: target_soc=%.2f kWh | energy_needed=%.2f kWh",
                            target_kwh, needed,
                        )
                # Effective capacity per slot (site_capacity - forecast)
                if forecast_data and time_slots:
                    logger.info("[OPTIMIZER INPUT] Effective capacity (site - forecast) per slot:")
                    for t_idx, slot_time in enumerate(time_slots[:5]):  # first 5
                        fd = forecast_data.get(slot_time, 0.0)
                        cap = max(0.0, site_capacity_kw - fd)
                        logger.info("  slot %s %s: %.2f kW", t_idx, slot_time.isoformat(), cap)
                    if n_slots > 5:
                        logger.info("  ... (%s more slots)", n_slots - 5)
                # ----- End debug -----

                # Finite upper bounds (Hexaly does not accept float('inf'))
                def _max_charge_kw(v_idx: int) -> float:
                    state = vehicle_states.get(vehicles[v_idx].vehicle_id)
                    return state.ac_charge_rate_kw if state else site_capacity_kw
                def _max_cumulative_kwh(v_idx: int) -> float:
                    state = vehicle_states.get(vehicles[v_idx].vehicle_id)
                    if state:
                        return max(0.0, state.battery_capacity_kwh - state.current_soc_kwh)
                    return 1000.0
                
                # ===== DECISION VARIABLES =====
                
                # charge_power[t][v]: Charging power (kW) for vehicle v at time t
                charge_power = [
                    [model.float(0, _max_charge_kw(v_idx)) for v_idx in range(n_vehicles)]
                    for _ in range(n_slots)
                ]
                
                # cumulative_energy[t][v]: Cumulative energy (kWh) delivered by time t
                cumulative_energy = [
                    [model.float(0, _max_cumulative_kwh(v_idx)) for v_idx in range(n_vehicles)]
                    for _ in range(n_slots)
                ]
                
                # ===== OBJECTIVE FUNCTION =====
                
                cost_terms = []
                for t_idx, slot_time in enumerate(time_slots):
                    # Get electricity price and TRIAD flag
                    price, is_triad = price_data.get(slot_time, (0.15, False))
                    
                    # Synthetic time price (earlier is cheaper)
                    synthetic_price = synthetic_time_price_factor * (n_slots - t_idx) / n_slots
                    
                    # TRIAD penalty
                    triad_cost = triad_penalty_factor if is_triad else 0.0
                    
                    # Total cost for this time slot
                    slot_cost = price + synthetic_price + triad_cost
                    
                    for v_idx in range(n_vehicles):
                        # Energy delivered in this slot (kWh) = power (kW) × 0.5 hours
                        energy_this_slot = charge_power[t_idx][v_idx] * 0.5
                        cost_terms.append(slot_cost * energy_this_slot)
                
                model.minimize(model.sum(cost_terms))
                
                # ===== CONSTRAINTS =====
                
                logger.info("Adding constraints...")
                
                # 1. Cumulative Energy Calculation
                for v_idx, vehicle in enumerate(vehicles):
                    state = vehicle_states.get(vehicle.vehicle_id)
                    if not state:
                        continue
                    
                    for t_idx in range(n_slots):
                        if t_idx == 0:
                            # First slot: cumulative = energy delivered in slot 0
                            model.constraint(
                                cumulative_energy[t_idx][v_idx] == 
                                charge_power[t_idx][v_idx] * 0.5
                            )
                        else:
                            # Subsequent slots: add to previous cumulative
                            model.constraint(
                                cumulative_energy[t_idx][v_idx] == 
                                cumulative_energy[t_idx - 1][v_idx] + 
                                charge_power[t_idx][v_idx] * 0.5
                            )
                
                # 2. Route Energy Requirements (EQ-ROUTE)
                for vehicle in vehicles:
                    v_idx = vehicle_to_idx[vehicle.vehicle_id]
                    state = vehicle_states.get(vehicle.vehicle_id)
                    requirements = energy_requirements.get(vehicle.vehicle_id, [])
                    
                    if not state or not requirements:
                        continue
                    
                    for requirement in requirements:
                        # Find time slot index for this checkpoint
                        checkpoint_idx = self._find_time_slot_index(
                            requirement.plan_start_date_time, time_slots
                        )
                        
                        if checkpoint_idx is None:
                            logger.warning(f"Vehicle {vehicle.vehicle_id}: "
                                         f"Checkpoint at {requirement.plan_start_date_time} "
                                         f"not in time slots")
                            continue
                        
                        # At departure time, must have sufficient energy
                        # cumulative_energy + initial_soc >= required_cumulative_energy
                        # Also enforce minimum charge level (e.g. 75%)
                        if checkpoint_idx > 0:
                            required_for_route = max(
                                0.0,
                                requirement.cumulative_energy_kwh - state.current_soc_kwh
                            )
                            min_soc_kwh = (min_soc_percent / 100.0) * state.battery_capacity_kwh
                            required_for_min_soc = max(
                                0.0,
                                min_soc_kwh - state.current_soc_kwh
                            )
                            required_energy = max(required_for_route, required_for_min_soc)
                            
                            model.constraint(
                                cumulative_energy[checkpoint_idx - 1][v_idx] >= 
                                required_energy
                            )
                            
                            logger.debug(f"Vehicle {vehicle.vehicle_id}: "
                                       f"Route {requirement.route_id} at slot {checkpoint_idx} "
                                       f"requires {required_energy:.2f} kWh")
                
                # 3. Target SOC for vehicles without routes
                for vehicle in vehicles:
                    v_idx = vehicle_to_idx[vehicle.vehicle_id]
                    state = vehicle_states.get(vehicle.vehicle_id)
                    requirements = energy_requirements.get(vehicle.vehicle_id, [])
                    
                    if not state:
                        continue
                    
                    if not requirements:
                        # No routes - charge to at least min_soc_percent, and up to target_soc_percent
                        effective_target_percent = max(target_soc_percent, min_soc_percent)
                        target_energy_kwh = (effective_target_percent / 100.0) * state.battery_capacity_kwh
                        energy_needed = target_energy_kwh - state.current_soc_kwh
                        
                        if energy_needed > 0:
                            model.constraint(
                                cumulative_energy[n_slots - 1][v_idx] >= energy_needed
                            )
                            
                            logger.debug(f"Vehicle {vehicle.vehicle_id}: "
                                       f"Target {energy_needed:.2f} kWh by end of window")
                
                # 4. Site Capacity Constraint (EQ-02)
                for t_idx, slot_time in enumerate(time_slots):
                    # Get forecasted site demand
                    site_demand_kw = forecast_data.get(slot_time, 0.0)
                    
                    # Total charging power across all vehicles
                    total_charging = model.sum(charge_power[t_idx])
                    
                    # Available capacity = site capacity - forecasted demand
                    available_capacity = site_capacity_kw - site_demand_kw

                    model.constraint(
                        total_charging <= max(0.0, available_capacity)
                    )

                    if available_capacity > 0:
                        model.constraint(total_charging <= available_capacity)
                
                # 5. Maximum SOC (EQ-04)
                for v_idx, vehicle in enumerate(vehicles):
                    state = vehicle_states.get(vehicle.vehicle_id)
                    if not state:
                        continue
                    
                    max_energy_kwh = state.battery_capacity_kwh - state.current_soc_kwh
                    
                    for t_idx in range(n_slots):
                        model.constraint(
                            cumulative_energy[t_idx][v_idx] <= max_energy_kwh
                        )
                
                # 6. Minimum SOC (EQ-05) - Already at 0 by variable bounds
                
                # 7. Charge Rate Limits (EQ-07, EQ-08)
                for v_idx, vehicle in enumerate(vehicles):
                    state = vehicle_states.get(vehicle.vehicle_id)
                    if not state:
                        continue
                    
                    # Use AC charge rate (DC charger logic can be added)
                    max_charge_rate = state.ac_charge_rate_kw
                    
                    for t_idx in range(n_slots):
                        model.constraint(
                            charge_power[t_idx][v_idx] <= max_charge_rate
                        )
                
                # 8. Vehicle Availability (EQ-09)
                for v_idx, vehicle in enumerate(vehicles):
                    availability = availability_matrices.get(vehicle.vehicle_id)
                    if not availability:
                        continue
                    
                    for t_idx in range(n_slots):
                        if (
                            not availability
                            or t_idx >= len(availability.availability_matrix)
                            or not availability.availability_matrix[t_idx]
                        ):
                            model.constraint(charge_power[t_idx][v_idx] == 0)

                
                # ===== SOLVE =====
                
                logger.info(f"Solving optimization (time limit: {self.time_limit}s)...")
                
                model.close()  # Required: Hexaly only allows solve() when model is closed
                optimizer.param.time_limit = self.time_limit
                optimizer.solve()
                
                # ===== EXTRACT SOLUTION =====
                
                solve_time = optimizer.statistics.running_time
                status = str(optimizer.solution.status)
                
                logger.info(f"Optimization completed: {status}, Time: {solve_time:.2f}s")
                
                # Build vehicle schedules
                vehicle_schedules = []
                total_energy = 0.0
                total_cost = 0.0
                
                for v_idx, vehicle in enumerate(vehicles):
                    state = vehicle_states.get(vehicle.vehicle_id)
                    requirements = energy_requirements.get(vehicle.vehicle_id, [])
                    
                    if not state:
                        continue
                    
                    # Calculate target energy (respect minimum SOC)
                    min_soc_kwh = (min_soc_percent / 100.0) * state.battery_capacity_kwh
                    if requirements:
                        route_target = (requirements[-1].cumulative_energy_kwh +
                                        state.current_soc_kwh)
                        target_energy_kwh = max(route_target, min_soc_kwh)
                    else:
                        effective_target = max(target_soc_percent, min_soc_percent)
                        target_energy_kwh = (effective_target / 100.0) * state.battery_capacity_kwh
                    
                    # Extract charge slots
                    charge_slots = []
                    cumulative = 0.0
                    
                    for t_idx, slot_time in enumerate(time_slots):
                        power_kw = charge_power[t_idx][v_idx].value
                        
                        if power_kw > 0.01:  # Skip negligible charging
                            energy_kwh = power_kw * 0.5
                            cumulative += energy_kwh
                            
                            price, is_triad = price_data.get(slot_time, (0.15, False))
                            site_demand = forecast_data.get(slot_time, 0.0)
                            
                            charge_slot = ChargeSlot(
                                time_slot=slot_time,
                                charge_power_kw=power_kw,
                                cumulative_energy_kwh=cumulative,
                                electricity_price=price,
                                site_demand_kw=site_demand,
                                is_triad_period=is_triad
                            )
                            
                            charge_slots.append(charge_slot)
                            total_energy += energy_kwh
                            total_cost += energy_kwh * price
                    
                    vehicle_schedule = VehicleChargeSchedule(
                        vehicle_id=vehicle.vehicle_id,
                        schedule_id=schedule_id,
                        initial_soc_kwh=state.current_soc_kwh,
                        target_soc_kwh=target_energy_kwh,
                        total_energy_needed_kwh=max(0, target_energy_kwh - state.current_soc_kwh),
                        route_checkpoints=requirements,
                        has_routes=len(requirements) > 0,
                        charge_slots=charge_slots,
                        total_energy_scheduled_kwh=cumulative,
                        assigned_charger_id=state.charger_id,
                        charger_type=state.charger_type
                    )
                    
                    vehicle_schedules.append(vehicle_schedule)
                
                return {
                    'vehicle_schedules': vehicle_schedules,
                    'total_cost': total_cost,
                    'total_energy_kwh': total_energy,
                    'solve_time_seconds': solve_time,
                    'status': status
                }
                
        except Exception as e:
            logger.error(f"Hexaly optimization failed: {str(e)}", exc_info=True)
            # Fall back to greedy
            return self._greedy_fallback(
                schedule_id, vehicles, vehicle_states, energy_requirements,
                availability_matrices, time_slots, price_data, target_soc_percent,
                min_soc_percent
            )
    
    def _find_time_slot_index(self, target_time: datetime, 
                              time_slots: List[datetime]) -> int:
        """Find index of time slot closest to target time."""
        for idx, slot_time in enumerate(time_slots):
            if slot_time >= target_time:
                return idx
        return None
    
    def _greedy_fallback(
        self,
        schedule_id: int,
        vehicles: List[Vehicle],
        vehicle_states: Dict[int, VehicleChargeState],
        energy_requirements: Dict[int, List[RouteEnergyRequirement]],
        availability_matrices: Dict[int, VehicleAvailability],
        time_slots: List[datetime],
        price_data: Dict[datetime, Tuple[float, bool]],
        target_soc_percent: float,
        min_soc_percent: Optional[float] = None
    ) -> Dict:
        """
        Greedy fallback algorithm when Hexaly unavailable.
        
        Charges each vehicle during available low-cost periods.
        """
        if min_soc_percent is None:
            min_soc_percent = DEFAULT_MIN_SOC_PERCENT
        logger.warning("Using greedy fallback algorithm")
        
        vehicle_schedules = []
        total_cost = 0.0
        total_energy = 0.0
        
        for vehicle in vehicles:
            state = vehicle_states.get(vehicle.vehicle_id)
            if not state:
                continue
            
            requirements = energy_requirements.get(vehicle.vehicle_id, [])
            availability = availability_matrices.get(vehicle.vehicle_id)
            
            # Calculate target energy (at least min_soc_percent)
            min_soc_kwh = (min_soc_percent / 100.0) * state.battery_capacity_kwh
            if requirements:
                route_target = (requirements[-1].cumulative_energy_kwh +
                                state.current_soc_kwh)
                target_energy_kwh = max(route_target, min_soc_kwh)
            else:
                effective_target = max(target_soc_percent, min_soc_percent)
                target_energy_kwh = (effective_target / 100.0) * state.battery_capacity_kwh
            
            energy_needed = target_energy_kwh - state.current_soc_kwh
            
            if energy_needed <= 0:
                vehicle_schedule = VehicleChargeSchedule(
                    vehicle_id=vehicle.vehicle_id,
                    schedule_id=schedule_id,
                    initial_soc_kwh=state.current_soc_kwh,
                    target_soc_kwh=target_energy_kwh,
                    total_energy_needed_kwh=0,
                    route_checkpoints=requirements,
                    has_routes=len(requirements) > 0
                )
                vehicle_schedules.append(vehicle_schedule)
                continue
            
            # Sort time slots by price (ascending)
            slot_prices = []
            for idx, slot_time in enumerate(time_slots):
                if availability and availability.availability_matrix[idx]:
                    price, is_triad = price_data.get(slot_time, (0.15, False))
                    # Penalize TRIAD periods
                    effective_price = price + (100.0 if is_triad else 0.0)
                    slot_prices.append((effective_price, idx, slot_time, price, is_triad))
            
            slot_prices.sort(key=lambda x: x[0])
            
            # Charge in cheapest available slots
            charge_slots = []
            cumulative = 0.0
            charge_rate = state.ac_charge_rate_kw
            
            for _, idx, slot_time, price, is_triad in slot_prices:
                if cumulative >= energy_needed:
                    break
                
                energy_this_slot = min(charge_rate * 0.5, energy_needed - cumulative)
                power_this_slot = energy_this_slot / 0.5
                cumulative += energy_this_slot
                
                charge_slot = ChargeSlot(
                    time_slot=slot_time,
                    charge_power_kw=power_this_slot,
                    cumulative_energy_kwh=cumulative,
                    electricity_price=price,
                    is_triad_period=is_triad
                )
                
                charge_slots.append(charge_slot)
                total_cost += energy_this_slot * price
                total_energy += energy_this_slot
            
            # Sort slots by time
            charge_slots.sort(key=lambda x: x.time_slot)
            
            vehicle_schedule = VehicleChargeSchedule(
                vehicle_id=vehicle.vehicle_id,
                schedule_id=schedule_id,
                initial_soc_kwh=state.current_soc_kwh,
                target_soc_kwh=target_energy_kwh,
                total_energy_needed_kwh=energy_needed,
                route_checkpoints=requirements,
                has_routes=len(requirements) > 0,
                charge_slots=charge_slots,
                total_energy_scheduled_kwh=cumulative,
                assigned_charger_id=state.charger_id,
                charger_type=state.charger_type
            )
            
            vehicle_schedules.append(vehicle_schedule)
        
        return {
            'vehicle_schedules': vehicle_schedules,
            'total_cost': total_cost,
            'total_energy_kwh': total_energy,
            'solve_time_seconds': 0.1,
            'status': 'greedy_fallback'
        }
