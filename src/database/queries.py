"""SQL queries for database operations."""
from datetime import datetime, timedelta


class Queries:
    """Repository for SQL queries."""
    
    # Allocation Monitor Queries
    CREATE_ALLOCATION_MONITOR = """
        INSERT INTO t_allocation_monitor (
            site_id, status, trigger_type, run_datetime,
            allocation_window_start, allocation_window_end
        ) VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING allocation_id
    """
    
    UPDATE_ALLOCATION_MONITOR = """
        UPDATE t_allocation_monitor
        SET status = %s, score = %s, routes_in_window = %s,
            routes_allocated = %s, routes_overlapping_count = %s
        WHERE allocation_id = %s
    """
    
    # Route Plan Queries
    GET_ROUTES_IN_WINDOW = """
        SELECT 
            route_id, site_id, vehicle_id, route_status, route_alias,
            plan_start_date_time, actual_start_date_time,
            plan_end_date_time, actual_end_date_time,
            plan_mileage, n_orders
        FROM t_route_plan
        WHERE site_id = %s
            AND route_status = 'N'
            AND plan_start_date_time >= %s
            AND plan_start_date_time <= %s
        ORDER BY plan_start_date_time ASC
    """
    
    # Vehicle Queries
    GET_ACTIVE_VEHICLES = '''
        SELECT 
            v.vehicle_id, v.site_id, v.active, v."VOR",
            v.charge_power_ac, v.charge_power_dc,
            v.battery_capacity, v.efficiency_kwh_mile,
            vt.telematic_label
        FROM t_vehicle v
        LEFT JOIN t_vehicle_telematics vt 
            ON v.vehicle_id = vt.vehicle_id AND vt.telematic_id = 2
        WHERE v.site_id = %s
            AND v.active = true
            AND v."VOR" = false
    '''
    
    # Vehicle State Management Queries
    GET_LATEST_VSM = """
        SELECT 
            vehicle_id, date_time, status, route_id,
            estimated_soc, return_eta, return_soc
        FROM t_vsm
        WHERE vehicle_id = %s
        ORDER BY date_time DESC
        LIMIT 1
    """
    
    GET_VSM_AS_OF = """
        SELECT 
            vehicle_id, date_time, status, route_id,
            estimated_soc, return_eta, return_soc
        FROM t_vsm
        WHERE vehicle_id = %s
            AND date_time <= %s
        ORDER BY date_time DESC
        LIMIT 1
    """
    
    GET_ALL_VSM_FOR_SITE = """
        SELECT DISTINCT ON (vsm.vehicle_id)
            vsm.vehicle_id, vsm.date_time, vsm.status, vsm.route_id,
            vsm.estimated_soc, vsm.return_eta, vsm.return_soc
        FROM t_vsm vsm
        INNER JOIN t_vehicle v ON vsm.vehicle_id = v.vehicle_id
        WHERE v.site_id = %s
        ORDER BY vsm.vehicle_id, vsm.date_time DESC
    """
    
    # Vehicle Charge Queries
    GET_VEHICLE_CHARGER = """
        SELECT charger_id, start_date_time
        FROM t_vehicle_charge
        WHERE vehicle_id = %s
        ORDER BY start_date_time DESC
        LIMIT 1
    """
    
    GET_VEHICLE_CHARGERS_IN_WINDOW = """
        WITH latest_charges AS (
            SELECT DISTINCT ON (vehicle_id)
                vehicle_id,
                charger_id,
                start_date_time
            FROM t_vehicle_charge
            WHERE vehicle_id = ANY(%s)
                AND start_date_time < %s
                AND start_date_time > %s - interval '18 hours'
            ORDER BY vehicle_id, start_date_time DESC
        )
        SELECT vehicle_id, charger_id, start_date_time
        FROM latest_charges
    """
    
    # Charger Queries
    GET_SITE_CHARGERS = """
        SELECT charger_id, site_id, max_power, dc_flag
        FROM t_charger
        WHERE site_id = %s
    """
    
    # Route Allocated Queries
    GET_EXISTING_ALLOCATIONS = """
        SELECT 
            route_id, vehicle_id_allocated, status,
            estimated_arrival, estimated_arrival_soc
        FROM t_route_allocated
        WHERE site_id = %s
            AND route_id = ANY(%s)
    """
    
    DELETE_SITE_ALLOCATIONS = """
        DELETE FROM t_route_allocated
        WHERE site_id = %s
    """
    
    INSERT_ROUTE_ALLOCATED = """
        INSERT INTO t_route_allocated (
            allocation_id, route_id, site_id, vehicle_id_allocated,
            status, estimated_arrival, estimated_arrival_soc,
            http_response, vehicle_id_actual
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    
    INSERT_ROUTE_ALLOCATED_HISTORY = """
        INSERT INTO t_route_allocated_history (
            allocation_id, route_id, site_id, vehicle_id_allocated,
            status, estimated_arrival, estimated_arrival_soc,
            http_response, vehicle_id_actual
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    
    # MAF Stored Procedure
    CALL_GET_MODULE_PARAMS = """
        SELECT sp_get_module_params(%s)
    """
    
    # Alert Queries
    INSERT_ALERT = """
        INSERT INTO t_alert (
            site_id, alert_message_id, dev_app_id, alert_date_time
        ) VALUES (%s, %s, %s, %s)
    """
    
    # Error Log Queries
    INSERT_ERROR_LOG = """
        INSERT INTO t_error_log (
            error_datetime, module_no, error_message
        ) VALUES (%s, %s, %s)
    """
    
    # ===== SCHEDULER QUERIES =====
    
    # Scheduler Configuration Queries
    CREATE_SCHEDULER = """
        INSERT INTO t_scheduler (
            device_id, schedule_type, status, run_datetime,
            planning_window_hours, route_energy_safety_factor,
            min_departure_buffer_minutes, back_to_back_threshold_minutes,
            target_soc_percent, agreed_site_capacity_kva,
            power_factor, site_usage_factor, max_fast_chargers,
            time_limit_seconds, created_date_time
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING schedule_id
    """
    
    GET_SCHEDULER_CONFIG = """
        SELECT 
            schedule_id, device_id as site_id, schedule_type, status, run_datetime,
            planning_window_hours, route_energy_safety_factor,
            min_departure_buffer_minutes, back_to_back_threshold_minutes,
            target_soc_percent, battery_factor, agreed_site_capacity_kva,
            power_factor, site_usage_factor, max_fast_chargers,
            time_limit_seconds, triad_penalty_factor, synthetic_time_price_factor,
            created_date_time, actual_planning_window_hours
        FROM t_scheduler
        WHERE schedule_id = %s
    """
    
    UPDATE_SCHEDULER_STATUS = """
        UPDATE t_scheduler
        SET status = %s, actual_planning_window_hours = %s
        WHERE schedule_id = %s
    """
    
    # Route Plan Queries for Scheduler (multi-route)
    GET_ROUTES_FOR_SCHEDULING_ROUTE_PLAN = """
        SELECT 
            route_id, site_id, vehicle_id, route_status, route_alias,
            plan_start_date_time, actual_start_date_time,
            plan_end_date_time, actual_end_date_time,
            plan_mileage, n_orders
        FROM t_route_plan
        WHERE vehicle_id = %s
            AND plan_start_date_time BETWEEN %s AND %s
            AND route_status IN ('N', 'A')
        ORDER BY plan_start_date_time ASC
    """
    
    GET_ROUTES_FOR_SCHEDULING_ALLOCATED = """
        SELECT 
            rp.route_id, rp.site_id, ra.vehicle_id_allocated as vehicle_id,
            rp.route_status, rp.route_alias,
            rp.plan_start_date_time, rp.actual_start_date_time,
            rp.plan_end_date_time, rp.actual_end_date_time,
            rp.plan_mileage, rp.n_orders
        FROM t_route_plan rp
        INNER JOIN t_route_allocated ra ON rp.route_id = ra.route_id
        WHERE ra.vehicle_id_allocated = %s
            AND rp.plan_start_date_time BETWEEN %s AND %s
            AND rp.route_status IN ('N', 'A')
        ORDER BY rp.plan_start_date_time ASC
    """
    
    GET_ALL_VEHICLES_FOR_SCHEDULING = """
        SELECT 
            v.vehicle_id, v.site_id, v.active, v."VOR",
            v.charge_power_ac, v.charge_power_dc,
            v.battery_capacity, v.efficiency_kwh_mile,
            vt.telematic_label
        FROM t_vehicle v
        LEFT JOIN t_vehicle_telematics vt 
            ON v.vehicle_id = vt.vehicle_id AND vt.telematic_id = 2
        WHERE v.site_id = %s
    """
    
    # Fleet Efficiency Calculation
    GET_FLEET_EFFICIENCY = """
        SELECT 
            COUNT(*) as vehicle_count,
            AVG(efficiency_kwh_mile) as fleet_avg_efficiency
        FROM t_vehicle
        WHERE site_id = %s
            AND efficiency_kwh_mile IS NOT NULL
    """
    
    # Forecast and Price Data Horizon
    GET_FORECAST_HORIZON = """
        SELECT MAX(forecasted_date_time) as max_forecast_time
        FROM t_site_energy_forecast_history
        WHERE site_id = %s
    """
    
    GET_PRICE_HORIZON = """
        SELECT MAX(date_time) as max_price_time
        FROM t_multisite_electricity_price
    """
    
    GET_FORECAST_DATA = """
        SELECT 
            forecasted_date_time,
            forecasted_energy_consumption_kw
        FROM t_site_energy_forecast_history
        WHERE site_id = %s
            AND forecasted_date_time BETWEEN %s AND %s
        ORDER BY forecasted_date_time ASC
    """
    
    GET_PRICE_DATA = """
        SELECT 
            date_time,
            electricity_price_gbp_kwh,
            is_triad_period
        FROM t_multisite_electricity_price
        WHERE date_time BETWEEN %s AND %s
        ORDER BY date_time ASC
    """
    
    # Charge Schedule Results
    INSERT_CHARGE_SCHEDULE = """
        INSERT INTO t_charge_schedule (
            schedule_id, vehicle_id, time_slot, charge_power_kw,
            cumulative_energy_kwh, electricity_price, site_demand_kw,
            is_triad_period, charger_id, charger_type,
            created_date_time
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    
    DELETE_CHARGE_SCHEDULE_BY_SCHEDULE_ID = """
        DELETE FROM t_charge_schedule
        WHERE schedule_id = %s
    """
    
    # Route Checkpoints
    INSERT_ROUTE_CHECKPOINT = """
        INSERT INTO t_schedule_route_checkpoints (
            schedule_id, vehicle_id, route_id,
            checkpoint_datetime_utc, required_cumulative_energy_kwh,
            route_energy_buffer_kwh, efficiency_used_kwh_mile,
            created_date_time
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """
    
    GET_ROUTE_CHECKPOINTS = """
        SELECT 
            checkpoint_id, schedule_id, vehicle_id, route_id,
            checkpoint_datetime_utc, required_cumulative_energy_kwh,
            route_energy_buffer_kwh, efficiency_used_kwh_mile
        FROM t_schedule_route_checkpoints
        WHERE schedule_id = %s
        ORDER BY vehicle_id, checkpoint_datetime_utc
    """
    
    # Vehicle State for Scheduling
    GET_VEHICLE_CHARGE_STATE = """
        SELECT 
            v.vehicle_id,
            v.battery_capacity,
            v.charge_power_ac,
            v.charge_power_dc,
            v.efficiency_kwh_mile,
            vsm.estimated_soc,
            vsm.status,
            vsm.route_id as current_route_id,
            vsm.return_eta,
            vsm.return_soc,
            vc.charger_id,
            c.dc_flag as is_dc_charger
        FROM t_vehicle v
        LEFT JOIN t_vsm vsm ON v.vehicle_id = vsm.vehicle_id
            AND vsm.date_time = (
                SELECT MAX(date_time) 
                FROM t_vsm 
                WHERE vehicle_id = v.vehicle_id
            )
        LEFT JOIN t_vehicle_charge vc ON v.vehicle_id = vc.vehicle_id
            AND vc.start_date_time = (
                SELECT MAX(start_date_time)
                FROM t_vehicle_charge
                WHERE vehicle_id = v.vehicle_id
            )
        LEFT JOIN t_charger c ON vc.charger_id = c.charger_id
        WHERE v.vehicle_id = %s
    """
    
    # Stale Schedule Detection
    GET_STALE_SCHEDULES = """
        SELECT schedule_id, device_id as site_id, created_date_time
        FROM t_scheduler
        WHERE status = 'completed'
            AND created_date_time < NOW() - INTERVAL '2 hours'
        ORDER BY created_date_time DESC
    """
