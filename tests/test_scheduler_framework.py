"""Testing framework for charge scheduler system."""
import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.controllers.scheduler_controller import SchedulerController
from src.models.scheduler import RouteSourceMode
from src.utils.logging_config import logger


class SchedulerTestFramework:
    """Framework for testing charge scheduler with custom scenarios."""
    
    def __init__(self):
        """Initialize test framework."""
        self.test_results = []
    
    def run_test_scenario(
        self, 
        site_id: int, 
        current_time: datetime,
        route_source_mode: Optional[RouteSourceMode] = None,
        custom_config: Optional[Dict] = None
    ) -> Dict:
        """
        Run scheduler test scenario.
        
        Args:
            site_id: Site identifier
            current_time: Current timestamp for scheduling
            route_source_mode: Route source mode (ROUTE_PLAN_ONLY or ALLOCATED_ROUTES)
            custom_config: Optional custom configuration override
        
        Returns:
            Test result dictionary
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"SCHEDULER TEST SCENARIO: Site {site_id}")
        logger.info(f"Current Time: {current_time}")
        logger.info(f"Route Source: {route_source_mode.value if route_source_mode else 'default'}")
        logger.info(f"{'='*60}\n")
        
        try:
            # Initialize controller
            controller = SchedulerController(site_id=site_id)
            
            # Override configuration if provided
            if custom_config:
                for key, value in custom_config.items():
                    if hasattr(controller.config, key):
                        setattr(controller.config, key, value)
                logger.info("Applied custom configuration")
            
            # Run scheduling
            start = datetime.now()
            result = controller.run_scheduling(
                current_time=current_time,
                route_source_mode=route_source_mode
            )
            end = datetime.now()
            
            execution_time = (end - start).total_seconds()
            
            # Build test result
            test_result = {
                'site_id': site_id,
                'current_time': current_time.isoformat(),
                'route_source_mode': route_source_mode.value if route_source_mode else 'default',
                'schedule_id': result.schedule_id,
                'status': result.optimization_status,
                'total_cost': result.total_cost,
                'total_energy_kwh': result.total_energy_kwh,
                'vehicles_scheduled': len(result.vehicle_schedules),
                'vehicles_with_routes': sum(1 for v in result.vehicle_schedules if v.has_routes),
                'vehicles_without_routes': sum(1 for v in result.vehicle_schedules if not v.has_routes),
                'planning_window_hours': result.actual_planning_window_hours,
                'planning_start': result.planning_start.isoformat(),
                'planning_end': result.planning_end.isoformat(),
                'execution_time_seconds': execution_time,
                'solve_time_seconds': result.solve_time_seconds,
                'optimization_status': result.optimization_status,
                'total_charge_slots': sum(len(v.charge_slots) for v in result.vehicle_schedules),
                'success': result.optimization_status == 'completed'
            }
            
            # Add vehicle details
            vehicle_details = []
            for vehicle_schedule in result.vehicle_schedules:
                vehicle_details.append({
                    'vehicle_id': vehicle_schedule.vehicle_id,
                    'has_routes': vehicle_schedule.has_routes,
                    'route_count': len(vehicle_schedule.route_checkpoints),
                    'initial_soc_kwh': vehicle_schedule.initial_soc_kwh,
                    'target_soc_kwh': vehicle_schedule.target_soc_kwh,
                    'energy_needed_kwh': vehicle_schedule.total_energy_needed_kwh,
                    'energy_scheduled_kwh': vehicle_schedule.total_energy_scheduled_kwh,
                    'charge_slots': len(vehicle_schedule.charge_slots),
                    'charger_id': vehicle_schedule.assigned_charger_id,
                    'charger_type': vehicle_schedule.charger_type
                })
            
            test_result['vehicle_details'] = vehicle_details
            
            self.test_results.append(test_result)
            
            # Print summary
            self._print_test_summary(test_result)
            
            controller.close()
            
            return test_result
        
        except Exception as e:
            logger.error(f"Test failed: {e}", exc_info=True)
            
            test_result = {
                'site_id': site_id,
                'current_time': current_time.isoformat(),
                'status': 'FAILED',
                'error': str(e),
                'success': False
            }
            
            self.test_results.append(test_result)
            return test_result
    
    def run_multiple_scenarios(self, scenarios: List[Dict]) -> List[Dict]:
        """
        Run multiple test scenarios.
        
        Args:
            scenarios: List of scenario configurations
        
        Returns:
            List of test results
        """
        logger.info(f"\n{'#'*60}")
        logger.info(f"RUNNING {len(scenarios)} SCHEDULER TEST SCENARIOS")
        logger.info(f"{'#'*60}\n")
        
        results = []
        
        for i, scenario in enumerate(scenarios, 1):
            logger.info(f"\n--- Scenario {i}/{len(scenarios)} ---")
            result = self.run_test_scenario(**scenario)
            results.append(result)
        
        # Print overall summary
        self._print_overall_summary(results)
        
        return results
    
    def _print_test_summary(self, result: Dict):
        """Print test result summary."""
        print("\n" + "="*60)
        print("SCHEDULER TEST RESULT SUMMARY")
        print("="*60)
        print(f"Site ID:              {result.get('site_id')}")
        print(f"Schedule ID:          {result.get('schedule_id')}")
        print(f"Status:               {result.get('status')}")
        print(f"Success:              {'✓' if result.get('success') else '✗'}")
        print(f"Total Cost:           £{result.get('total_cost', 0):.2f}")
        print(f"Total Energy:         {result.get('total_energy_kwh', 0):.2f} kWh")
        print(f"Vehicles Scheduled:   {result.get('vehicles_scheduled', 0)}")
        print(f"  With Routes:        {result.get('vehicles_with_routes', 0)}")
        print(f"  Without Routes:     {result.get('vehicles_without_routes', 0)}")
        print(f"Planning Window:      {result.get('planning_window_hours', 0):.1f} hours")
        print(f"Total Charge Slots:   {result.get('total_charge_slots', 0)}")
        print(f"Optimization Status:  {result.get('optimization_status', 'N/A')}")
        print(f"Solve Time:           {result.get('solve_time_seconds', 0):.2f}s")
        print(f"Execution Time:       {result.get('execution_time_seconds', 0):.2f}s")
        
        if result.get('vehicle_details'):
            print("\nVehicle Details:")
            for vehicle in result['vehicle_details'][:5]:  # Show first 5
                print(f"  Vehicle {vehicle['vehicle_id']}: "
                      f"{vehicle['energy_scheduled_kwh']:.1f}/{vehicle['energy_needed_kwh']:.1f} kWh, "
                      f"{vehicle['charge_slots']} slots, "
                      f"{'routes' if vehicle['has_routes'] else 'target SOC'}")
            
            if len(result['vehicle_details']) > 5:
                print(f"  ... and {len(result['vehicle_details']) - 5} more vehicles")
        
        print("="*60 + "\n")
    
    def _print_overall_summary(self, results: List[Dict]):
        """Print overall test summary."""
        print("\n" + "#"*60)
        print("OVERALL SCHEDULER TEST SUMMARY")
        print("#"*60)
        
        total = len(results)
        successful = sum(1 for r in results if r.get('success'))
        failed = total - successful
        
        print(f"Total Scenarios:      {total}")
        print(f"Successful:           {successful}")
        print(f"Failed:               {failed}")
        print(f"Success Rate:         {(successful/total*100):.1f}%")
        
        if successful > 0:
            avg_cost = sum(r.get('total_cost', 0) for r in results if r.get('success')) / successful
            avg_energy = sum(r.get('total_energy_kwh', 0) for r in results if r.get('success')) / successful
            avg_solve_time = sum(r.get('solve_time_seconds', 0) for r in results if r.get('success')) / successful
            avg_exec_time = sum(r.get('execution_time_seconds', 0) for r in results if r.get('success')) / successful
            
            print(f"\nAverage Cost:         £{avg_cost:.2f}")
            print(f"Average Energy:       {avg_energy:.2f} kWh")
            print(f"Average Solve Time:   {avg_solve_time:.2f}s")
            print(f"Average Exec Time:    {avg_exec_time:.2f}s")
        
        print("#"*60 + "\n")
    
    def export_results(self, filename: str = 'scheduler_test_results.json'):
        """
        Export test results to JSON file.
        
        Args:
            filename: Output filename
        """
        with open(filename, 'w') as f:
            json.dump(self.test_results, f, indent=2)
        
        logger.info(f"Test results exported to {filename}")


def create_sample_scenarios() -> List[Dict]:
    """
    Create sample test scenarios.
    
    Returns:
        List of scenario configurations
    """
    # Use current date: February 12, 2026
    base_time = datetime(2026, 2, 12, 4, 30, 0)  # 4:30 AM
    
    scenarios = [
        {
            'site_id': 10,
            'current_time': base_time,
            'route_source_mode': RouteSourceMode.ROUTE_PLAN_ONLY
        },
        {
            'site_id': 10,
            'current_time': base_time,
            'route_source_mode': RouteSourceMode.ALLOCATED_ROUTES
        },
        {
            'site_id': 10,
            'current_time': base_time + timedelta(hours=6),
            'route_source_mode': RouteSourceMode.ROUTE_PLAN_ONLY
        },
        {
            'site_id': 10,
            'current_time': base_time + timedelta(hours=12),
            'route_source_mode': RouteSourceMode.ALLOCATED_ROUTES,
            'custom_config': {
                'planning_window_hours': 12.0,
                'target_soc_percent': 90.0
            }
        }
    ]
    
    return scenarios


def main():
    """Main test runner."""
    parser = argparse.ArgumentParser(description='Charge Scheduler Test Framework')
    parser.add_argument('--site-id', type=int, help='Site ID to test')
    parser.add_argument('--current-time', type=str, help='Current time (YYYY-MM-DD HH:MM:SS)')
    parser.add_argument('--route-source', type=str, choices=['route_plan_only', 'allocated_routes'],
                       help='Route source mode')
    parser.add_argument('--sample-scenarios', action='store_true', help='Run sample scenarios')
    parser.add_argument('--export', type=str, help='Export results to file')
    
    args = parser.parse_args()
    
    framework = SchedulerTestFramework()
    
    if args.sample_scenarios:
        # Run sample scenarios
        scenarios = create_sample_scenarios()
        results = framework.run_multiple_scenarios(scenarios)
    elif args.site_id and args.current_time:
        # Run single scenario
        current_time = datetime.strptime(args.current_time, '%Y-%m-%d %H:%M:%S')
        
        route_source_mode = None
        if args.route_source == 'route_plan_only':
            route_source_mode = RouteSourceMode.ROUTE_PLAN_ONLY
        elif args.route_source == 'allocated_routes':
            route_source_mode = RouteSourceMode.ALLOCATED_ROUTES
        
        result = framework.run_test_scenario(
            site_id=args.site_id,
            current_time=current_time,
            route_source_mode=route_source_mode
        )
    else:
        parser.print_help()
        return
    
    # Export results if requested
    if args.export:
        framework.export_results(args.export)


if __name__ == '__main__':
    main()
