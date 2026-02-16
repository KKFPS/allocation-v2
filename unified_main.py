"""Entry point for unified vehicle allocation and charge scheduling optimization."""
import argparse
from datetime import datetime
from src.controllers.unified_controller import UnifiedController
from src.utils.logging_config import logger


def main():
    """Main entry point for unified optimization."""
    parser = argparse.ArgumentParser(
        description='Unified Vehicle Allocation and Charge Scheduling Optimizer',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Run integrated optimization (allocation + scheduling)
  python unified_main.py --site-id 10 --mode integrated
  
  # Run allocation only
  python unified_main.py --site-id 10 --mode allocation_only
  
  # Run scheduling only
  python unified_main.py --site-id 10 --mode scheduling_only
  
  # Run with custom start time
  python unified_main.py --site-id 10 --mode integrated \\
      --start-time "2026-02-16 04:30:00"
  
  # Run without database persistence
  python unified_main.py --site-id 10 --mode integrated --no-persist
        '''
    )
    
    parser.add_argument(
        '--site-id',
        type=int,
        required=True,
        help='Site identifier'
    )
    
    parser.add_argument(
        '--mode',
        type=str,
        default='integrated',
        choices=['allocation_only', 'allocation', 'scheduling_only', 'scheduling', 'integrated', 'both'],
        help='Optimization mode (default: integrated)'
    )
    
    parser.add_argument(
        '--start-time',
        type=str,
        help='Start time for optimization (YYYY-MM-DD HH:MM:SS). Defaults to now.'
    )
    
    parser.add_argument(
        '--trigger-type',
        type=str,
        default='initial',
        help='Allocation trigger type (default: initial)'
    )
    
    parser.add_argument(
        '--schedule-id',
        type=int,
        help='Existing schedule ID (for scheduling_only mode)'
    )
    
    parser.add_argument(
        '--no-persist',
        action='store_true',
        help='Do not persist results to database'
    )
    
    args = parser.parse_args()
    
    # Parse start time
    if args.start_time:
        start_time = datetime.strptime(args.start_time, '%Y-%m-%d %H:%M:%S')
    else:
        start_time = None
    
    # Initialize controller
    controller = UnifiedController(
        site_id=args.site_id,
        trigger_type=args.trigger_type,
        schedule_id=args.schedule_id
    )
    
    try:
        logger.info(f"Starting unified optimization for site {args.site_id} in {args.mode} mode")
        
        # Run optimization
        allocation_result, schedule_result, unified_result = controller.run_unified_optimization(
            current_time=start_time,
            mode=args.mode,
            config=None,  # Uses defaults
            persist_to_database=not args.no_persist
        )
        
        # Print results
        print("\n" + "="*70)
        print("UNIFIED OPTIMIZATION COMPLETED")
        print("="*70)
        print(f"Site ID:          {args.site_id}")
        print(f"Mode:             {args.mode}")
        print(f"Status:           {unified_result.status}")
        print(f"Objective Value:  {unified_result.objective_value:.2f}")
        print(f"Solve Time:       {unified_result.solve_time_seconds:.2f}s")
        
        # Allocation metrics
        if args.mode in ['allocation_only', 'allocation', 'integrated', 'both']:
            print("\nALLOCATION RESULTS:")
            print(f"  Routes Allocated: {unified_result.routes_allocated}/{unified_result.routes_total}")
            print(f"  Allocation Score: {unified_result.allocation_score:.2f}")
            print(f"  Allocation ID:    {controller.allocation_id}")
        
        # Scheduling metrics
        if args.mode in ['scheduling_only', 'scheduling', 'integrated', 'both']:
            print("\nSCHEDULING RESULTS:")
            print(f"  Total Energy:     {unified_result.total_energy_kwh:.2f} kWh")
            print(f"  Total Cost:       £{unified_result.total_charging_cost:.2f}")
            avg_cost = (unified_result.total_charging_cost / unified_result.total_energy_kwh 
                       if unified_result.total_energy_kwh > 0 else 0)
            print(f"  Avg Cost/kWh:     £{avg_cost:.4f}")
            print(f"  Schedule ID:      {controller.schedule_id}")
        
        print("="*70)
        
        if not args.no_persist:
            print("\n✓ Results persisted to database")
        
        logger.info("Unified optimization completed successfully")
        
    except Exception as e:
        logger.error(f"Unified optimization failed: {e}", exc_info=True)
        print(f"\n✗ Error: {e}")
        return 1
    
    finally:
        controller.close()
    
    return 0


if __name__ == '__main__':
    exit(main())
