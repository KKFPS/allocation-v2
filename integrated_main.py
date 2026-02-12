"""Main entry point for integrated allocation + scheduling workflow."""
import argparse
import sys
from datetime import datetime
from src.controllers.integrated_workflow import (
    IntegratedWorkflowController,
    run_allocation_only,
    run_scheduler_only,
    run_allocation_then_scheduling
)
from src.models.scheduler import RouteSourceMode
from src.utils.logging_config import logger


def main():
    """Main execution function for integrated workflow."""
    parser = argparse.ArgumentParser(
        description='Integrated Allocation and Scheduling System',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run full workflow (allocation + scheduling)
  python integrated_main.py --site-id 10
  
  # Run allocation only
  python integrated_main.py --site-id 10 --allocation-only
  
  # Run scheduler only (independent mode, using route_plan)
  python integrated_main.py --site-id 10 --scheduling-only
  
  # Run scheduler only (using allocated routes)
  python integrated_main.py --site-id 10 --scheduling-only --use-allocated-routes
  
  # Run with custom trigger and planning window
  python integrated_main.py --site-id 10 --trigger cancellation --planning-window 12
  
  # Skip allocation, run scheduling on existing allocations
  python integrated_main.py --site-id 10 --skip-allocation --use-allocated-routes
        """
    )
    
    parser.add_argument(
        '--site-id',
        type=int,
        required=True,
        help='Site ID for workflow'
    )
    
    # Workflow mode selection
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        '--allocation-only',
        action='store_true',
        help='Run allocation only (skip scheduling)'
    )
    
    mode_group.add_argument(
        '--scheduling-only',
        action='store_true',
        help='Run scheduling only (skip allocation)'
    )
    
    # Allocation parameters
    parser.add_argument(
        '--trigger',
        type=str,
        default='initial',
        choices=['initial', 'cancellation', 'arrival', 'estimated_arrival', 'different_allocation'],
        help='Allocation trigger type (default: initial)'
    )
    
    # Scheduling parameters
    parser.add_argument(
        '--planning-window',
        type=float,
        help='Planning window in hours for scheduler (4-24, default: 18)'
    )
    
    parser.add_argument(
        '--use-allocated-routes',
        action='store_true',
        help='Use t_route_allocated for vehicle-route mapping (default: use t_route_plan.vehicle_id)'
    )
    
    parser.add_argument(
        '--skip-allocation',
        action='store_true',
        help='Skip allocation step (use existing allocations for scheduling)'
    )
    
    # Time override
    parser.add_argument(
        '--current-time',
        type=str,
        help='Override current time (format: YYYY-MM-DD HH:MM:SS, default: now UTC)'
    )
    
    # Logging
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
        
        # Validate planning window
        if args.planning_window and not (4.0 <= args.planning_window <= 24.0):
            logger.error(f"Planning window must be between 4 and 24 hours, got {args.planning_window}")
            sys.exit(1)
        
        # Determine route source mode
        route_source_mode = (RouteSourceMode.ALLOCATED_ROUTES 
                           if args.use_allocated_routes 
                           else RouteSourceMode.ROUTE_PLAN_ONLY)
        
        # Execute workflow based on mode
        if args.allocation_only:
            # Allocation only
            logger.info(f"Running ALLOCATION ONLY for site {args.site_id}")
            result = run_allocation_only(
                site_id=args.site_id,
                trigger_type=args.trigger,
                current_time=current_time
            )
            
            print("\n" + "="*80)
            print("ALLOCATION COMPLETED")
            print("="*80)
            print(f"Allocation ID:        {result.allocation_id}")
            print(f"Site ID:              {result.site_id}")
            print(f"Routes Allocated:     {result.routes_allocated}")
            print(f"Routes in Window:     {result.routes_in_window}")
            print(f"Score:                {result.score}")
            print(f"Status:               {result.status}")
            print(f"Trigger:              {args.trigger}")
            print("="*80 + "\n")
            
            sys.exit(0)
            
        elif args.scheduling_only:
            # Scheduling only
            logger.info(f"Running SCHEDULING ONLY for site {args.site_id}")
            logger.info(f"Route source mode: {route_source_mode.value}")
            
            result = run_scheduler_only(
                site_id=args.site_id,
                current_time=current_time,
                route_source_mode=route_source_mode
            )
            
            print("\n" + "="*80)
            print("SCHEDULING COMPLETED")
            print("="*80)
            print(f"Schedule ID:          {result.schedule_id}")
            print(f"Site ID:              {result.site_id}")
            print(f"Planning Window:      {result.actual_planning_window_hours:.1f} hours")
            print(f"Vehicles Scheduled:   {result.vehicles_scheduled}")
            print(f"Routes Considered:    {result.routes_considered}")
            print(f"Total Energy:         {result.total_energy_kwh:.2f} kWh")
            print(f"Total Cost:           £{result.total_cost:.2f}")
            print(f"Validation Passed:    {'✓ YES' if result.validation_passed else '✗ NO'}")
            print("="*80 + "\n")
            
            sys.exit(0 if result.validation_passed else 1)
            
        else:
            # Full integrated workflow
            logger.info(f"Running INTEGRATED WORKFLOW for site {args.site_id}")
            
            controller = IntegratedWorkflowController(
                site_id=args.site_id,
                trigger_type=args.trigger
            )
            
            workflow_result = controller.run_integrated_workflow(
                current_time=current_time,
                planning_window_hours=args.planning_window,
                skip_allocation=args.skip_allocation,
                skip_scheduling=False
            )
            
            # Print summary
            print("\n" + controller.get_summary())
            
            if workflow_result['success']:
                # Check validation
                schedule_result = workflow_result.get('schedule_result')
                validation_passed = schedule_result.validation_passed if schedule_result else True
                sys.exit(0 if validation_passed else 1)
            else:
                print(f"\nERROR: {workflow_result.get('error', 'Unknown error')}", file=sys.stderr)
                sys.exit(1)
        
    except KeyboardInterrupt:
        logger.info("\nWorkflow interrupted by user")
        sys.exit(130)
        
    except Exception as e:
        logger.error(f"Workflow failed: {str(e)}", exc_info=True)
        print(f"\nERROR: {str(e)}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
