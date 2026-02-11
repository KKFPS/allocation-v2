"""Testing framework for allocation system."""
import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.controllers.allocation_controller import AllocationController
from src.models.vehicle import Vehicle
from src.models.route import Route
from src.utils.logging_config import logger


class AllocationTestFramework:
    """Framework for testing allocation with custom scenarios."""
    
    def __init__(self):
        """Initialize test framework."""
        self.test_results = []
    
    def run_test_scenario(self, site_id: int, start_time: datetime, 
                         window_hours: int = 18, trigger_type: str = 'initial',
                         custom_config: Optional[Dict] = None) -> Dict:
        """
        Run allocation test scenario.
        
        Args:
            site_id: Site identifier
            start_time: Allocation start time
            window_hours: Window duration in hours
            trigger_type: Type of allocation trigger
            custom_config: Optional custom MAF configuration override
        
        Returns:
            Test result dictionary
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"TEST SCENARIO: Site {site_id}")
        logger.info(f"Start Time: {start_time}")
        logger.info(f"Window: {window_hours} hours")
        logger.info(f"Trigger: {trigger_type}")
        logger.info(f"{'='*60}\n")
        
        try:
            # Initialize controller
            controller = AllocationController(site_id, trigger_type)
            
            # Override configuration if provided
            if custom_config:
                controller.site_config = custom_config
                logger.info("Applied custom configuration")
            
            # Run allocation
            start = datetime.now()
            result = controller.run_allocation(start_time)
            end = datetime.now()
            
            execution_time = (end - start).total_seconds()
            
            # Build test result
            test_result = {
                'site_id': site_id,
                'start_time': start_time.isoformat(),
                'window_hours': window_hours,
                'trigger_type': trigger_type,
                'allocation_id': result.allocation_id,
                'status': result.status,
                'total_score': result.total_score,
                'routes_in_window': result.routes_in_window,
                'routes_allocated': result.routes_allocated,
                'routes_unallocated': len(result.unallocated_routes),
                'execution_time_seconds': execution_time,
                'vehicle_sequences': result.get_vehicle_sequences(),
                'is_acceptable': result.is_acceptable(),
                'success': result.status == 'A'
            }
            
            self.test_results.append(test_result)
            
            # Print summary
            self._print_test_summary(test_result)
            
            controller.close()
            
            return test_result
        
        except Exception as e:
            logger.error(f"Test failed: {e}", exc_info=True)
            
            test_result = {
                'site_id': site_id,
                'start_time': start_time.isoformat(),
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
        logger.info(f"RUNNING {len(scenarios)} TEST SCENARIOS")
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
        print("TEST RESULT SUMMARY")
        print("="*60)
        print(f"Site ID:           {result.get('site_id')}")
        print(f"Allocation ID:     {result.get('allocation_id')}")
        print(f"Status:            {result.get('status')}")
        print(f"Success:           {'✓' if result.get('success') else '✗'}")
        print(f"Total Score:       {result.get('total_score', 0):.2f}")
        print(f"Routes in Window:  {result.get('routes_in_window', 0)}")
        print(f"Routes Allocated:  {result.get('routes_allocated', 0)}")
        print(f"Routes Unallocated: {result.get('routes_unallocated', 0)}")
        print(f"Execution Time:    {result.get('execution_time_seconds', 0):.2f}s")
        
        if result.get('vehicle_sequences'):
            print("\nVehicle Assignments:")
            for vehicle_id, route_ids in result['vehicle_sequences'].items():
                print(f"  Vehicle {vehicle_id}: {len(route_ids)} routes")
        
        print("="*60 + "\n")
    
    def _print_overall_summary(self, results: List[Dict]):
        """Print overall test summary."""
        print("\n" + "#"*60)
        print("OVERALL TEST SUMMARY")
        print("#"*60)
        
        total = len(results)
        successful = sum(1 for r in results if r.get('success'))
        failed = total - successful
        
        print(f"Total Scenarios:   {total}")
        print(f"Successful:        {successful}")
        print(f"Failed:            {failed}")
        print(f"Success Rate:      {(successful/total*100):.1f}%")
        
        if successful > 0:
            avg_score = sum(r.get('total_score', 0) for r in results if r.get('success')) / successful
            avg_time = sum(r.get('execution_time_seconds', 0) for r in results if r.get('success')) / successful
            print(f"\nAverage Score:     {avg_score:.2f}")
            print(f"Average Time:      {avg_time:.2f}s")
        
        print("#"*60 + "\n")
    
    def export_results(self, filename: str = 'test_results.json'):
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
    base_time = datetime(2026, 2, 11, 4, 30, 0)  # 4:30 AM
    
    scenarios = [
        {
            'site_id': 10,
            'start_time': base_time,
            'window_hours': 18,
            'trigger_type': 'initial'
        },
        {
            'site_id': 10,
            'start_time': base_time + timedelta(hours=6),
            'window_hours': 18,
            'trigger_type': 'arrival'
        },
        {
            'site_id': 10,
            'start_time': base_time + timedelta(hours=12),
            'window_hours': 18,
            'trigger_type': 'estimated_arrival'
        }
    ]
    
    return scenarios


def main():
    """Main test runner."""
    parser = argparse.ArgumentParser(description='Allocation System Test Framework')
    parser.add_argument('--site-id', type=int, help='Site ID to test')
    parser.add_argument('--start-time', type=str, help='Start time (YYYY-MM-DD HH:MM:SS)')
    parser.add_argument('--window-hours', type=int, default=18, help='Window duration in hours')
    parser.add_argument('--trigger-type', type=str, default='initial', help='Trigger type')
    parser.add_argument('--sample-scenarios', action='store_true', help='Run sample scenarios')
    parser.add_argument('--export', type=str, help='Export results to file')
    
    args = parser.parse_args()
    
    framework = AllocationTestFramework()
    
    if args.sample_scenarios:
        # Run sample scenarios
        scenarios = create_sample_scenarios()
        results = framework.run_multiple_scenarios(scenarios)
    elif args.site_id and args.start_time:
        # Run single scenario
        start_time = datetime.strptime(args.start_time, '%Y-%m-%d %H:%M:%S')
        result = framework.run_test_scenario(
            site_id=args.site_id,
            start_time=start_time,
            window_hours=args.window_hours,
            trigger_type=args.trigger_type
        )
    else:
        parser.print_help()
        return
    
    # Export results if requested
    if args.export:
        framework.export_results(args.export)


if __name__ == '__main__':
    main()
