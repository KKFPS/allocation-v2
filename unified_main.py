"""Entry point for unified route allocation optimization."""
import argparse
from datetime import datetime

from src.controllers.unified_controller import UnifiedController
from src.utils.logging_config import logger


def main():
    parser = argparse.ArgumentParser(
        description="Vehicle-route allocation optimizer",
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
        allocation_result, _schedule_result, solver_result = controller.run_unified_optimization(
            current_time=start_time,
            mode=["allocation"],
            persist_to_database=not args.no_persist,
            window_hours=args.window_hours,
        )

        print("\n" + "=" * 70)
        print("ROUTE ALLOCATION COMPLETED")
        print("=" * 70)
        print(f"Site ID:          {args.site_id}")
        if solver_result:
            print(f"Status:           {solver_result.status}")
            print(f"Objective:        {solver_result.objective_value:.2f}")
            print(
                f"Routes:           {solver_result.routes_allocated}/"
                f"{solver_result.routes_total}"
            )
            print(f"Solve Time:       {solver_result.solve_time_seconds:.2f}s")
        if allocation_result:
            print(f"Total Score:      {allocation_result.total_score:.2f}")
        print(f"Allocation ID:    {controller.allocation_id}")
        print("=" * 70)

        if not args.no_persist:
            print("\nResults persisted to database")

        logger.info("Route allocation completed successfully")
        return 0

    except Exception as e:
        logger.error("Route allocation failed: %s", e, exc_info=True)
        print(f"\nError: {e}")
        return 1
    finally:
        controller.close()


if __name__ == "__main__":
    raise SystemExit(main())
