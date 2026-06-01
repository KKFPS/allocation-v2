"""Optimizer package — route allocation and integrated scheduling."""

from src.optimizer.allocation_optimizer import (
    AllocationConfig,
    RouteAllocationOptimizer,
    RouteAllocationSolverResult,
)
from src.optimizer.cost_matrix import (
    AllocationDataBuilder,
    AllocationModelData,
    ChargeSchedulingContext,
    ModelDataBuilder,
    OptimizationModelData,
)
from src.optimizer.unified_optimizer import (
    MODE_FLAG_ALLOCATION,
    MODE_FLAG_CHARGE_SCHEDULING,
    MODE_FLAG_CHARGER_ALLOCATION,
    OptimizationConfig,
    OptimizationResult,
    UnifiedOptimizer,
    normalize_mode,
)

__all__ = [
    "AllocationConfig",
    "AllocationDataBuilder",
    "AllocationModelData",
    "ChargeSchedulingContext",
    "ModelDataBuilder",
    "OptimizationConfig",
    "OptimizationModelData",
    "OptimizationResult",
    "RouteAllocationOptimizer",
    "RouteAllocationSolverResult",
    "UnifiedOptimizer",
    "normalize_mode",
    "MODE_FLAG_ALLOCATION",
    "MODE_FLAG_CHARGE_SCHEDULING",
    "MODE_FLAG_CHARGER_ALLOCATION",
]
