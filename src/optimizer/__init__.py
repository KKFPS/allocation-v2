"""Optimizer package initialization."""
from src.optimizer.hexaly_solver import HexalySolver
from src.optimizer.charge_optimizer import ChargeOptimizer
from src.optimizer.unified_optimizer import (
    UnifiedOptimizer,
    UnifiedOptimizationConfig,
    UnifiedOptimizationResult,
    OptimizationMode,
    MODE_FLAG_ALLOCATION,
    MODE_FLAG_CHARGE_SCHEDULING,
    MODE_FLAG_CHARGER_ALLOCATION,
    normalize_mode_input,
    resolve_optimization_from_modes,
)

__all__ = [
    'HexalySolver',
    'ChargeOptimizer',
    'UnifiedOptimizer',
    'UnifiedOptimizationConfig',
    'UnifiedOptimizationResult',
    'OptimizationMode',
    'MODE_FLAG_ALLOCATION',
    'MODE_FLAG_CHARGE_SCHEDULING',
    'MODE_FLAG_CHARGER_ALLOCATION',
    'normalize_mode_input',
    'resolve_optimization_from_modes',
]
