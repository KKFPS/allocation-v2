"""Entry point for Phase 1 route allocation optimization."""
import argparse
from datetime import datetime

from src.controllers.unified_controller import UnifiedController
from src.utils.logging_config import logger


def main():
    parser = argparse.ArgumentParser(
        description="Phase 1 vehicle-route allocation optimizer",
    )
    parser.add_argument("--site-id", type=int, required=True, help="Site identifier")
    parser.add_argument(
        "--start-time",
        type=str,
        help="Start time (YYYY-MM-DD HH:MM:SS). Defaults to now.",
    )
    parser.add_argument(
        "--trigger-type",
        type=str,
        default="initial",
        help="Allocation trigger type",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Do not persist results to database",
    )
    parser.add_argument(
        "--window-hours",
        type=float,
        default=None,
        help="Planning window length in hours",
    )

    args = parser.parse_args()
    start_time = (
        datetime.strptime(args.start_time, "%Y-%m-%d %H:%M:%S")
        if args.start_time
        else None
    )

    controller = UnifiedController(
        site_id=args.site_id,
        trigger_type=args.trigger_type,
    )

    try:
        allocation_result, phase1_result = controller.run_unified_optimization(
            current_time=start_time,
            persist_to_database=not args.no_persist,
            window_hours=args.window_hours,
        )

        print("\n" + "=" * 70)
        print("PHASE 1 ALLOCATION COMPLETED")
        print("=" * 70)
        print(f"Site ID:          {args.site_id}")
        print(f"Status:           {phase1_result.status}")
        print(f"Objective:        {phase1_result.objective_value:.2f}")
        print(f"Routes:           {phase1_result.routes_allocated}/{phase1_result.routes_total}")
        print(f"Solve Time:       {phase1_result.solve_time_seconds:.2f}s")
        print(f"Allocation ID:    {controller.allocation_id}")
        print(f"Total Score:      {allocation_result.total_score:.2f}")
        print("=" * 70)

        if not args.no_persist:
            print("\nResults persisted to database")

        logger.info("Phase 1 allocation completed successfully")
        return 0

    except Exception as e:
        logger.error("Phase 1 allocation failed: %s", e, exc_info=True)
        print(f"\nError: {e}")
        return 1
    finally:
        controller.close()


if __name__ == "__main__":
    raise SystemExit(main())
