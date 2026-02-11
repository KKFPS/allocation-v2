"""Main entry point for vehicle-route allocation system."""
import argparse
import sys
from datetime import datetime
from src.controllers.allocation_controller import AllocationController
from src.utils.logging_config import logger


def main():
    """Main execution function."""
    parser = argparse.ArgumentParser(
        description='Vehicle-Route Allocation System',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run allocation for site 10
  python main.py --site-id 10
  
  # Run allocation with specific trigger type
  python main.py --site-id 10 --trigger-type cancellation
  
  # Run allocation for specific time
  python main.py --site-id 10 --start-time "2026-02-11 04:30:00"
  
  # Dry run (don't persist to database)
  python main.py --site-id 10 --dry-run
        """
    )
    
    parser.add_argument(
        '--site-id',
        type=int,
        required=True,
        help='Site ID for allocation'
    )
    
    parser.add_argument(
        '--trigger-type',
        type=str,
        default='initial',
        choices=['initial', 'cancellation', 'arrival', 'estimated_arrival', 'different_allocation'],
        help='Type of allocation trigger (default: initial)'
    )
    
    parser.add_argument(
        '--start-time',
        type=str,
        help='Start time for allocation (format: YYYY-MM-DD HH:MM:SS, default: now)'
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Run allocation without persisting to database'
    )
    
    args = parser.parse_args()
    
    # Parse start time
    if args.start_time:
        try:
            start_time = datetime.strptime(args.start_time, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            logger.error("Invalid start time format. Use: YYYY-MM-DD HH:MM:SS")
            sys.exit(1)
    else:
        start_time = datetime.now()
    
    # Display configuration
    logger.info("="*60)
    logger.info("VEHICLE-ROUTE ALLOCATION SYSTEM")
    logger.info("="*60)
    logger.info(f"Site ID:       {args.site_id}")
    logger.info(f"Trigger Type:  {args.trigger_type}")
    logger.info(f"Start Time:    {start_time}")
    logger.info(f"Dry Run:       {args.dry_run}")
    logger.info("="*60)
    
    try:
        # Initialize controller
        controller = AllocationController(
            site_id=args.site_id,
            trigger_type=args.trigger_type
        )
        
        # Run allocation
        result = controller.run_allocation(start_time)
        
        # Display results
        print("\n" + "="*60)
        print("ALLOCATION RESULTS")
        print("="*60)
        print(f"Allocation ID:      {result.allocation_id}")
        print(f"Status:             {result.status}")
        print(f"Total Score:        {result.total_score:.2f}")
        print(f"Routes in Window:   {result.routes_in_window}")
        print(f"Routes Allocated:   {result.routes_allocated}")
        print(f"Routes Unallocated: {len(result.unallocated_routes)}")
        print(f"Acceptable:         {'Yes' if result.is_acceptable() else 'No'}")
        
        if result.is_acceptable():
            print("\n✓ Allocation completed successfully")
            
            # Show vehicle assignments
            print("\nVehicle Assignments:")
            vehicle_sequences = result.get_vehicle_sequences()
            for vehicle_id, route_ids in vehicle_sequences.items():
                print(f"  Vehicle {vehicle_id}: {len(route_ids)} route(s)")
        else:
            print("\n✗ Allocation rejected due to low quality score")
        
        print("="*60 + "\n")
        
        # Close controller
        controller.close()
        
        # Exit code
        sys.exit(0 if result.is_acceptable() else 1)
    
    except KeyboardInterrupt:
        logger.info("\nAllocation interrupted by user")
        sys.exit(130)
    
    except Exception as e:
        logger.error(f"Allocation failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
