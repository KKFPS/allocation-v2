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
