"""Main entry point for charge scheduler system."""
import argparse
import sys
from datetime import datetime
from src.controllers.scheduler_controller import SchedulerController
from src.models.scheduler import RouteSourceMode
from src.utils.logging_config import logger


def main():
    """Main execution function for scheduler."""
    parser = argparse.ArgumentParser(
        description='Charge Scheduler System',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run scheduler for site 10 (creates new schedule)
  python scheduler_main.py --site-id 10
  
  # Run existing schedule by ID
  python scheduler_main.py --schedule-id 5001
  
  # Run with allocated routes mode
  python scheduler_main.py --site-id 10 --route-source allocated
  
  # Run with specific planning window
  python scheduler_main.py --site-id 10 --planning-window 12
  
  # Dry run (don't persist to database) - NOT YET IMPLEMENTED
  python scheduler_main.py --site-id 10 --dry-run
        """
    )
    
    # Mutually exclusive: either schedule-id or site-id required
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        '--schedule-id',
        type=int,
        help='Existing schedule ID to execute'
    )
    
    group.add_argument(
        '--site-id',
        type=int,
        help='Site ID for new schedule'
    )
    
    parser.add_argument(
        '--route-source',
        type=str,
        default='route_plan',
        choices=['route_plan', 'allocated'],
        help='Route source mode: "route_plan" uses t_route_plan.vehicle_id, '
             '"allocated" uses t_route_plan JOIN t_route_allocated (default: route_plan)'
    )
    
    parser.add_argument(
        '--planning-window',
        type=float,
        help='Planning window in hours (4-24, default: 18)'
    )
    
    parser.add_argument(
        '--current-time',
        type=str,
        help='Override current time (format: YYYY-MM-DD HH:MM:SS, default: now UTC)'
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Execute without persisting results (NOT YET IMPLEMENTED)'
    )
    
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Enable verbose logging'
    )
    
    args = parser.parse_args()
    
    # Set logging level
    if args.verbose:
        logger.setLevel('DEBUG')
    
    try:
        # Parse current time if provided
        current_time = None
        if args.current_time:
            try:
                current_time = datetime.strptime(args.current_time, '%Y-%m-%d %H:%M:%S')
                logger.info(f"Using override current time: {current_time}")
            except ValueError as e:
                logger.error(f"Invalid time format: {args.current_time}")
                sys.exit(1)
        
        # Parse route source mode
        route_source_mode = (RouteSourceMode.ALLOCATED_ROUTES 
                            if args.route_source == 'allocated' 
                            else RouteSourceMode.ROUTE_PLAN_ONLY)
        
        # Check dry run
        if args.dry_run:
            logger.warning("Dry run mode not yet implemented - will persist results")
        
        # Initialize controller
        if args.schedule_id:
            logger.info(f"Running existing schedule: {args.schedule_id}")
            controller = SchedulerController(schedule_id=args.schedule_id)
        else:
            logger.info(f"Creating new schedule for site: {args.site_id}")
            controller = SchedulerController(site_id=args.site_id)
            
            # Override planning window if provided
            if args.planning_window:
                if not (4.0 <= args.planning_window <= 24.0):
                    logger.error(f"Planning window must be between 4 and 24 hours, got {args.planning_window}")
                    sys.exit(1)
                logger.info(f"Using custom planning window: {args.planning_window} hours")
        
        # Run scheduling
        result = controller.run_scheduling(
            current_time=current_time,
            route_source_mode=route_source_mode
        )
        
        # Print summary
        print("\n" + "="*80)
        print("CHARGE SCHEDULING COMPLETED")
        print("="*80)
        print(f"Schedule ID:              {result.schedule_id}")
        print(f"Site ID:                  {result.site_id}")
        print(f"Planning Window:          {result.planning_start} to {result.planning_end}")
        print(f"Actual Window Hours:      {result.actual_planning_window_hours:.1f}")
        print(f"Vehicles Scheduled:       {result.vehicles_scheduled}")
        print(f"Routes Considered:        {result.routes_considered}")
        print(f"Route Checkpoints:        {result.checkpoints_created}")
        print(f"Total Energy:             {result.total_energy_kwh:.2f} kWh")
        print(f"Total Cost:               £{result.total_cost:.2f}")
        print(f"Optimization Time:        {result.solve_time_seconds:.2f} seconds")
        print(f"Optimization Status:      {result.optimization_status}")
        print(f"Validation Passed:        {'✓ YES' if result.validation_passed else '✗ NO'}")
        
        if result.validation_errors:
            print("\nValidation Errors:")
            for error in result.validation_errors:
                print(f"  ✗ {error}")
        
        if result.validation_warnings:
            print("\nValidation Warnings:")
            for warning in result.validation_warnings:
                print(f"  ⚠ {warning}")
        
        print("\nVehicle Schedules:")
        print("-" * 80)
        for vs in result.vehicle_schedules:
            status = "✓" if vs.meets_route_requirements else "✗"
            print(f"{status} Vehicle {vs.vehicle_id:3d}: "
                  f"Initial {vs.initial_soc_kwh:5.1f} kWh → Target {vs.target_soc_kwh:5.1f} kWh | "
                  f"Charge {vs.total_energy_scheduled_kwh:5.1f} kWh in {len(vs.charge_slots)} slots | "
                  f"Routes: {len(vs.route_checkpoints)}")
            
            if not vs.meets_route_requirements:
                print(f"    Energy shortfall: {vs.energy_shortfall_kwh:.2f} kWh")
        
        print("="*80 + "\n")
        
        # Exit with appropriate code
        sys.exit(0 if result.validation_passed else 1)
        
    except KeyboardInterrupt:
        logger.info("\nScheduling interrupted by user")
        sys.exit(130)
        
    except Exception as e:
        logger.error(f"Scheduling failed: {str(e)}", exc_info=True)
        print(f"\nERROR: {str(e)}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
