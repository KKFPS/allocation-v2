"""MAF parameter parsing module."""
import json
import traceback
from datetime import datetime
from typing import Any, Dict, Optional
from src.utils.logging_config import logger
from src.config import DEFAULT_PENALTIES, DEFAULT_CONSTRAINT_ENABLED

def parse_maf_parameter(param_key: str, param_value: str) -> Any:
    """
    Parse MAF string parameter to appropriate type.
    
    All MAF parameters are stored as {String: String}. This function
    infers the correct type based on parameter naming conventions and
    value patterns.
    
    Args:
        param_key: Parameter name (e.g., "constraint_turnaround_time_strict_enabled")
        param_value: String value (e.g., "true", "45", "[1, 2, 3]")
    
    Returns:
        Parsed value in appropriate type
    """
    # Handle None/empty
    if param_value in ['NONE', 'None', 'none', 'NO_VALUE', '', None]:
        return None
    
    # Boolean detection
    if param_key.endswith('_enabled') or param_key.endswith('_flag') or \
       param_value.lower() in ['true', 'false', 'yes', 'no']:
        return param_value.lower() in ['true', 'yes', '1']
    
    # JSON array
    if param_value.strip().startswith('['):
        try:
            return json.loads(param_value)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON array for {param_key}: {param_value} - {e}")
            return param_value
    
    # JSON object
    if param_value.strip().startswith('{'):
        try:
            return json.loads(param_value)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON object for {param_key}: {param_value} - {e}")
            return param_value
    
    # Numeric detection
    numeric_suffixes = ['_minutes', '_hours', '_seconds', '_kwh', '_penalty', 
                        '_weight', '_bonus', '_threshold', '_count', '_margin']
    if any(param_key.endswith(suffix) for suffix in numeric_suffixes):
        try:
            if '.' not in param_value:
                return int(param_value)
            else:
                return float(param_value)
        except ValueError as e:
            logger.error(f"Failed to parse numeric for {param_key}: {param_value} - {e}")
            return None
    
    # Time format detection
    if ':' in param_value and param_key.endswith('_period'):
        try:
            return datetime.strptime(param_value, '%H:%M:%S').time()
        except ValueError as e:
            logger.error(f"Failed to parse time for {param_key}: {param_value} - {e}")
            return None
    
    # Default: string
    return param_value


def get_constraint_config(site_id: int, constraint_name: str, maf_params: Dict[str, str]) -> Dict[str, Any]:
    """
    Retrieve constraint configuration from MAF parameters.
    
    Args:
        site_id: Site identifier
        constraint_name: Name of constraint (e.g., "energy_feasibility")
        maf_params: Dictionary of MAF parameters
    
    Returns:
        Dictionary with 'enabled' (bool), 'params' (dict), 'penalty' (numeric)
    """
    enabled_key = f"constraint_{constraint_name}_enabled"
    enabled_value = str(maf_params.get(enabled_key, DEFAULT_CONSTRAINT_ENABLED.get(constraint_name, "true")))
    enabled = parse_maf_parameter(enabled_key, enabled_value)
    
    if not enabled:
        logger.info(f"Constraint '{constraint_name}' disabled for site {site_id}")
        return {'enabled': False, 'params': {}, 'penalty': 0}
    
    # Extract all parameters for this constraint
    constraint_params = {}
    prefix = f"constraint_{constraint_name}_"
    # logger.info(f"Parsing MAF parameters for constraint '{constraint_name}': {maf_params}")
    for key, value in maf_params.items():
        # logger.info(f"Parsing MAF parameter: {key} = {value}")
        if key.startswith(prefix) and key != enabled_key:
            param_name = key[len(prefix):]
            constraint_params[param_name] = parse_maf_parameter(key, value)
    
    penalty = constraint_params.get('penalty', DEFAULT_PENALTIES.get(constraint_name, -20))
    
    logger.info(f"Constraint '{constraint_name}' enabled for site {site_id} with {len(constraint_params)} parameters")
    
    return {
        'enabled': True,
        'params': constraint_params,
        'penalty': penalty
    }


def parse_maf_response(maf_json: Dict) -> Dict[int, Dict[str, Any]]:
    """
    Parse MAF stored procedure response into site-specific configurations.
    
    Args:
        maf_json: JSON response from sp_get_module_params
    
    Returns:
        Dictionary mapping site_id to configuration parameters
    """
    site_configs = {}
    
    try:
        clients = maf_json.get('clients', [])
        logger.info(f"Clients: {clients}")
        for client in clients:
            try:
                sites = client.get('sites', [])
                
                for site in sites:
                    try:
                        site_id = site.get('site_id')
                        if not site_id:
                            continue

                        # Parse site-level parameters
                        site_params = {}
                        parameters = site.get('parameters', {})

                        logger.info(f"Site parameters: {parameters}")
                        for param in parameters:
                            # logger.info(f"Parsing MAF parameter: {param.get('parameter_name')} = {param.get('parameter_value')}")
                            site_params[param.get('parameter_name')] = parse_maf_parameter(param.get('parameter_name'), param.get('parameter_value'))
                        
                        # Parse vehicle-specific parameters
                        vehicles = site.get('vehicles', [])
                        enabled_vehicles = []
                        
                        for vehicle in vehicles:
                            vehicle_id = vehicle.get('vehicle_id')
                            enabled = parse_maf_parameter('enabled', vehicle.get('enabled', 'true'))
                            
                            if enabled and vehicle_id:
                                enabled_vehicles.append(vehicle_id)

                        logger.info(f"Enabled vehicles: {enabled_vehicles}")
                        # logger.info(f"Site parameters: {site_params}")
                        
                        site_configs[site_id] = {
                            'parameters': site_params,
                            'enabled_vehicles': enabled_vehicles
                        }

                        # logger.info(f"Loaded MAF config for site {site_id}: {site_configs[site_id]}")
                        
                        # logger.info(f"Loaded MAF config for site {site_id}: {len(site_params)} parameters, {len(enabled_vehicles)} enabled vehicles")
                    except Exception as e:
                        logger.error(f"Failed to parse MAF config for site {site_id}: {e}")
                        continue
            except Exception as e:
                logger.error(f"Failed to parse MAF config for client {client.get('client_id')}: {e}")
                continue
        return site_configs
        
    except Exception as e:
        logger.error(f"Failed to parse MAF response: {e}")
        return {}


def get_site_parameter(site_config: Dict, param_key: str, default: Any = None) -> Any:
    """
    Get a site-specific parameter with fallback to default.
    
    Args:
        site_config: Site configuration dictionary
        param_key: Parameter key to retrieve
        default: Default value if parameter not found
    
    Returns:
        Parameter value or default
    """
    params = site_config.get('parameters', {})
    return params.get(param_key, default)


def get_all_constraint_configs(site_id: int, site_config: Dict) -> Dict[str, Dict]:
    """
    Get all constraint configurations for a site.
    
    Args:
        site_id: Site identifier
        site_config: Site configuration dictionary
    
    Returns:
        Dictionary mapping constraint names to their configurations
    """
    constraint_names = [
        'energy_feasibility',
        'turnaround_time_strict',
        'turnaround_time_preferred',
        'shift_hours_strict',
        'minimum_soonness',
        'route_overlap',
        'charger_preference',
        'swap_minimization',
        'energy_optimization'
    ]
    
    constraints = {}
    maf_params = site_config.get('parameters', {})

    logger.info(f"MAF params: {maf_params}")
    
    for constraint_name in constraint_names:
        constraints[constraint_name] = get_constraint_config(site_id, constraint_name, maf_params)
    
    return constraints
