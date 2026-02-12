"""Integration module for sequential allocation and scheduling."""
from datetime import datetime
from typing import Optional, Dict, Any
from src.controllers.allocation_controller import AllocationController
from src.controllers.scheduler_controller import SchedulerController
from src.models.scheduler import RouteSourceMode
from src.models.allocation import AllocationResult
from src.models.scheduler import ChargeScheduleResult
from src.utils.logging_config import logger


class IntegratedWorkflowController:
    """
    Controller for running allocation followed by scheduling.
    
    This provides a unified workflow where:
    1. Allocation assigns vehicles to routes
    2. Scheduler creates charge plans based on allocated routes
    """
    
    def __init__(self, site_id: int, trigger_type: str = 'initial'):
        """
        Initialize integrated workflow controller.
        
        Args:
            site_id: Site identifier
            trigger_type: Type of allocation trigger
        """
        self.site_id = site_id
        self.trigger_type = trigger_type
        self.allocation_result: Optional[AllocationResult] = None
        self.schedule_result: Optional[ChargeScheduleResult] = None
    
    def run_integrated_workflow(
        self,
        current_time: Optional[datetime] = None,
        planning_window_hours: Optional[float] = None,
        skip_allocation: bool = False,
        skip_scheduling: bool = False
    ) -> Dict[str, Any]:
        """
        Execute allocation and scheduling workflow.
        
        Args:
            current_time: Current datetime (defaults to now)
            planning_window_hours: Override planning window for scheduler
            skip_allocation: Skip allocation step (use existing allocations)
            skip_scheduling: Skip scheduling step (allocation only)
        
        Returns:
            Dictionary with allocation_result and schedule_result
        """
        if current_time is None:
            current_time = datetime.utcnow()
        
        logger.info("="*80)
        logger.info(f"STARTING INTEGRATED WORKFLOW - Site {self.site_id}")
        logger.info(f"Trigger: {self.trigger_type}, Time: {current_time}")
        logger.info("="*80)
        
        try:
            # Step 1: Run Allocation
            if not skip_allocation:
                logger.info("\n" + "-"*80)
                logger.info("STEP 1: VEHICLE-ROUTE ALLOCATION")
                logger.info("-"*80)
                
                allocation_controller = AllocationController(
                    site_id=self.site_id,
                    trigger_type=self.trigger_type
                )
                
                self.allocation_result = allocation_controller.run_allocation(current_time)
                
                logger.info(f"Allocation completed - Allocation ID: {self.allocation_result.allocation_id}")
                logger.info(f"Routes allocated: {self.allocation_result.routes_allocated}")
                logger.info(f"Score: {self.allocation_result.score}")
            else:
                logger.info("\n" + "-"*80)
                logger.info("STEP 1: SKIPPED (Using existing allocations)")
                logger.info("-"*80)
            
            # Step 2: Run Scheduling
            if not skip_scheduling:
                logger.info("\n" + "-"*80)
                logger.info("STEP 2: CHARGE SCHEDULING")
                logger.info("-"*80)
                
                # Determine route source mode based on whether we ran allocation
                route_source_mode = (RouteSourceMode.ALLOCATED_ROUTES 
                                   if not skip_allocation 
                                   else RouteSourceMode.ROUTE_PLAN_ONLY)
                
                scheduler_controller = SchedulerController(site_id=self.site_id)
                
                # Override planning window if provided
                if planning_window_hours:
                    logger.info(f"Using custom planning window: {planning_window_hours} hours")
                
                self.schedule_result = scheduler_controller.run_scheduling(
                    current_time=current_time,
                    route_source_mode=route_source_mode
                )
                
                logger.info(f"Scheduling completed - Schedule ID: {self.schedule_result.schedule_id}")
                logger.info(f"Vehicles scheduled: {self.schedule_result.vehicles_scheduled}")
                logger.info(f"Total energy: {self.schedule_result.total_energy_kwh:.2f} kWh")
                logger.info(f"Total cost: £{self.schedule_result.total_cost:.2f}")
            else:
                logger.info("\n" + "-"*80)
                logger.info("STEP 2: SKIPPED")
                logger.info("-"*80)
            
            logger.info("\n" + "="*80)
            logger.info("INTEGRATED WORKFLOW COMPLETED SUCCESSFULLY")
            logger.info("="*80)
            
            return {
                'success': True,
                'allocation_result': self.allocation_result,
                'schedule_result': self.schedule_result,
                'allocation_id': self.allocation_result.allocation_id if self.allocation_result else None,
                'schedule_id': self.schedule_result.schedule_id if self.schedule_result else None
            }
            
        except Exception as e:
            logger.error(f"Integrated workflow failed: {str(e)}", exc_info=True)
            
            return {
                'success': False,
                'error': str(e),
                'allocation_result': self.allocation_result,
                'schedule_result': self.schedule_result
            }
    
    def get_summary(self) -> str:
        """Get human-readable summary of workflow results."""
        lines = []
        lines.append("="*80)
        lines.append("INTEGRATED WORKFLOW SUMMARY")
        lines.append("="*80)
        lines.append(f"Site ID: {self.site_id}")
        lines.append(f"Trigger Type: {self.trigger_type}")
        lines.append("")
        
        if self.allocation_result:
            lines.append("ALLOCATION RESULTS:")
            lines.append(f"  Allocation ID:        {self.allocation_result.allocation_id}")
            lines.append(f"  Routes Allocated:     {self.allocation_result.routes_allocated}")
            lines.append(f"  Routes in Window:     {self.allocation_result.routes_in_window}")
            lines.append(f"  Score:                {self.allocation_result.score}")
            lines.append(f"  Status:               {self.allocation_result.status}")
            lines.append("")
        
        if self.schedule_result:
            lines.append("SCHEDULING RESULTS:")
            lines.append(f"  Schedule ID:          {self.schedule_result.schedule_id}")
            lines.append(f"  Planning Window:      {self.schedule_result.actual_planning_window_hours:.1f} hours")
            lines.append(f"  Vehicles Scheduled:   {self.schedule_result.vehicles_scheduled}")
            lines.append(f"  Routes Considered:    {self.schedule_result.routes_considered}")
            lines.append(f"  Route Checkpoints:    {self.schedule_result.checkpoints_created}")
            lines.append(f"  Total Energy:         {self.schedule_result.total_energy_kwh:.2f} kWh")
            lines.append(f"  Total Cost:           £{self.schedule_result.total_cost:.2f}")
            lines.append(f"  Validation Passed:    {'✓ YES' if self.schedule_result.validation_passed else '✗ NO'}")
            lines.append("")
            
            if self.schedule_result.validation_errors:
                lines.append("  Validation Errors:")
                for error in self.schedule_result.validation_errors:
                    lines.append(f"    ✗ {error}")
                lines.append("")
        
        lines.append("="*80)
        
        return "\n".join(lines)


def run_allocation_only(site_id: int, trigger_type: str = 'initial',
                       current_time: Optional[datetime] = None) -> AllocationResult:
    """
    Convenience function to run allocation only.
    
    Args:
        site_id: Site identifier
        trigger_type: Allocation trigger type
        current_time: Override current time
    
    Returns:
        AllocationResult
    """
    controller = AllocationController(site_id=site_id, trigger_type=trigger_type)
    return controller.run_allocation(current_time)


def run_scheduler_only(site_id: Optional[int] = None,
                      schedule_id: Optional[int] = None,
                      current_time: Optional[datetime] = None,
                      route_source_mode: RouteSourceMode = RouteSourceMode.ROUTE_PLAN_ONLY) -> ChargeScheduleResult:
    """
    Convenience function to run scheduler only.
    
    Args:
        site_id: Site identifier (for new schedule)
        schedule_id: Existing schedule ID
        current_time: Override current time
        route_source_mode: Route source configuration
    
    Returns:
        ChargeScheduleResult
    """
    controller = SchedulerController(schedule_id=schedule_id, site_id=site_id)
    return controller.run_scheduling(current_time=current_time, route_source_mode=route_source_mode)


def run_allocation_then_scheduling(site_id: int,
                                  trigger_type: str = 'initial',
                                  current_time: Optional[datetime] = None,
                                  planning_window_hours: Optional[float] = None) -> Dict[str, Any]:
    """
    Convenience function to run both allocation and scheduling.
    
    Args:
        site_id: Site identifier
        trigger_type: Allocation trigger type
        current_time: Override current time
        planning_window_hours: Override scheduler planning window
    
    Returns:
        Dictionary with results
    """
    controller = IntegratedWorkflowController(site_id=site_id, trigger_type=trigger_type)
    return controller.run_integrated_workflow(
        current_time=current_time,
        planning_window_hours=planning_window_hours
    )
