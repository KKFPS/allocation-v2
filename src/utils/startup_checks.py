"""Startup configuration checks: environment variables and MAF parameters."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from src.config import APPLICATION_NAME
from src.database.connection import db
from src.database.queries import Queries
from src.config import DEFAULT_CONSTRAINT_ENABLED
from src.maf.parameter_parser import (
    get_site_parameter,
    parse_maf_parameter,
    parse_maf_response,
)
from src.utils.logging_config import logger

_REDACTED = "<redacted>"
_SENSITIVE_NAME_RE = re.compile(
    r"(password|pswd|secret|_key$|_token|conn_string|connection_string|credential)",
    re.IGNORECASE,
)

_SITE_LEVEL_MAF_PARAMS = (
    "allocation_window_hours",
    "max_routes_per_vehicle_in_window",
    "planning_window_hours",
    "target_soc_percent",
)

_CONSTRAINT_NAMES = (
    "energy_feasibility",
    "turnaround_time_strict",
    "turnaround_time_preferred",
    "shift_hours_strict",
    "minimum_soonness",
    "charger_preference",
    "swap_minimization",
    "energy_optimization",
)


@dataclass(frozen=True)
class EnvVarSpec:
    name: str
    required: bool = False
    sensitive: bool = False
    default: Optional[str] = None
    group: str = "general"


ENV_VAR_SPECS: Tuple[EnvVarSpec, ...] = (
    # Application
    EnvVarSpec("WEBSITE_SITE_NAME", default="vehicle_allocation_system", group="application"),
    EnvVarSpec("LOG_LEVEL", default="INFO", group="application"),
    # Database
    EnvVarSpec("psgrsql_db_user", required=True, group="database"),
    EnvVarSpec("psgrsql_db_pswd", required=True, sensitive=True, group="database"),
    EnvVarSpec("psgrsql_db_name", required=True, group="database"),
    EnvVarSpec("psgrsql_db_host", required=True, group="database"),
    EnvVarSpec("psgrsql_db_port", default="5432", group="database"),
    # Hexaly
    EnvVarSpec("HEXALY_CLOUD_KEY", sensitive=True, group="hexaly"),
    EnvVarSpec("HEXALY_CLOUD_SECRET", sensitive=True, group="hexaly"),
    EnvVarSpec("HEXALY_LOCAL_AVAILABLE", group="hexaly"),
    # DB clone scheduler
    EnvVarSpec("CLONE_DB_SCHEDULER_ENABLED", default="false", group="db_clone"),
    EnvVarSpec("CLONE_SITE_IDS", group="db_clone"),
    EnvVarSpec("CLONE_CLIENT_ID", group="db_clone"),
    # Optimizer scheduler
    EnvVarSpec("OPTIMIZER_SCHEDULER_ENABLED", default="false", group="optimizer_scheduler"),
    EnvVarSpec("OPTIMIZER_SITE_IDS", group="optimizer_scheduler"),
    EnvVarSpec("OPTIMIZER_CRON_MINUTE", default="15", group="optimizer_scheduler"),
    EnvVarSpec("OPTIMIZER_CRON_HOUR", default="*", group="optimizer_scheduler"),
    # Microlise
    EnvVarSpec("JLP_Microlise_TokenClientId", sensitive=True, group="microlise"),
    EnvVarSpec("JLP_Microlise_TokenClient_Secret", sensitive=True, group="microlise"),
    EnvVarSpec("JLP_Microlise_Token_URL", group="microlise"),
    EnvVarSpec("JLP_Microlise_JourneysWebAPI_URL", group="microlise"),
    EnvVarSpec("storage_account_conn_string", sensitive=True, group="microlise"),
    EnvVarSpec("allocation_blob_container", group="microlise"),
    EnvVarSpec("allocation_blob_dir", group="microlise"),
    EnvVarSpec("simulate_response", default="True", group="microlise"),
    EnvVarSpec("send_report", default="False", group="microlise"),
    # Clone source/destination DB overrides
    EnvVarSpec("SOURCE_psgrsql_db_host", group="db_clone"),
    EnvVarSpec("SOURCE_psgrsql_db_user", group="db_clone"),
    EnvVarSpec("SOURCE_psgrsql_db_pswd", sensitive=True, group="db_clone"),
    EnvVarSpec("SOURCE_psgrsql_db_name", group="db_clone"),
    EnvVarSpec("SOURCE_psgrsql_db_port", group="db_clone"),
    # EnvVarSpec("DEST_psgrsql_db_host", group="db_clone"),
    # EnvVarSpec("DEST_psgrsql_db_user", group="db_clone"),
    # EnvVarSpec("DEST_psgrsql_db_pswd", sensitive=True, group="db_clone"),
    # EnvVarSpec("DEST_psgrsql_db_name", group="db_clone"),
    # EnvVarSpec("DEST_psgrsql_db_port", group="db_clone"),
)


def _is_sensitive(name: str, spec_sensitive: bool = False) -> bool:
    return spec_sensitive or bool(_SENSITIVE_NAME_RE.search(name))


def _format_env_value(name: str, value: str, sensitive: bool) -> str:
    if sensitive:
        return _REDACTED
    if len(value) > 120:
        return f"{value[:117]}..."
    return value


def _env_is_set(value: Optional[str]) -> bool:
    return value is not None and value.strip() != ""


def check_env_configuration() -> Dict[str, List[str]]:
    """Log configured and unconfigured environment variables."""
    configured: List[str] = []
    unconfigured: List[str] = []
    missing_required: List[str] = []

    logger.info("=" * 60)
    logger.info("STARTUP: Environment variable check")
    logger.info("=" * 60)

    current_group: Optional[str] = None
    for spec in ENV_VAR_SPECS:
        if spec.group != current_group:
            current_group = spec.group
            logger.info("  [%s]", current_group)

        raw = os.getenv(spec.name)
        sensitive = _is_sensitive(spec.name, spec.sensitive)
        is_set = _env_is_set(raw)

        if is_set:
            display = _format_env_value(spec.name, raw, sensitive)
            configured.append(spec.name)
            logger.info("    %s = %s", spec.name, display)
        else:
            unconfigured.append(spec.name)
            if spec.required:
                missing_required.append(spec.name)
                logger.warning("    %s = NOT SET (required)", spec.name)
            elif spec.default is not None:
                logger.info("    %s = NOT SET (default: %s)", spec.name, spec.default)
            else:
                logger.info("    %s = NOT SET", spec.name)

    logger.info(
        "Environment summary: %d configured, %d unconfigured, %d missing required",
        len(configured),
        len(unconfigured),
        len(missing_required),
    )
    if missing_required:
        logger.warning("Missing required env vars: %s", ", ".join(missing_required))

    return {
        "configured": configured,
        "unconfigured": unconfigured,
        "missing_required": missing_required,
    }


def _extract_maf_payload(row: Dict[str, Any]) -> Any:
    payload = row.get("sp_get_module_params")
    if isinstance(payload, (list, tuple)) and len(payload) == 2:
        return payload[1]
    return payload


def _normalize_site_id(site_id: Any) -> Any:
    if isinstance(site_id, str) and site_id.isdigit():
        return int(site_id)
    return site_id


def _log_maf_site(site_id: Any, site_config: Dict[str, Any]) -> None:
    params = site_config.get("parameters", {})
    enabled_vehicles = site_config.get("enabled_vehicles", [])

    logger.info("  Site %s:", site_id)
    logger.info("    parameters: %d", len(params))
    logger.info("    enabled_vehicles: %d", len(enabled_vehicles))

    for key in _SITE_LEVEL_MAF_PARAMS:
        value = get_site_parameter(site_config, key, default=None)
        if value is not None:
            logger.info("    %s = %s", key, value)
        else:
            logger.info("    %s = NOT SET (will use code default)", key)

    enabled_constraints: List[str] = []
    disabled_constraints: List[str] = []
    for name in _CONSTRAINT_NAMES:
        enabled_key = f"constraint_{name}_enabled"
        default_enabled = DEFAULT_CONSTRAINT_ENABLED.get(name, True)
        raw = params.get(enabled_key, default_enabled)
        enabled = parse_maf_parameter(enabled_key, str(raw))
        if enabled:
            enabled_constraints.append(name)
        else:
            disabled_constraints.append(name)
    logger.info("    constraints enabled (%d): %s", len(enabled_constraints), ", ".join(enabled_constraints) or "none")
    if disabled_constraints:
        logger.info("    constraints disabled (%d): %s", len(disabled_constraints), ", ".join(disabled_constraints))


def check_maf_configuration() -> Dict[str, Any]:
    """Load and log MAF parameters from the database."""
    logger.info("=" * 60)
    logger.info("STARTUP: MAF parameter check (application=%s)", APPLICATION_NAME)
    logger.info("=" * 60)

    result: Dict[str, Any] = {
        "application_name": APPLICATION_NAME,
        "loaded": False,
        "site_count": 0,
        "error": None,
    }

    try:
        rows = db.execute_query(
            Queries.CALL_GET_MODULE_PARAMS,
            (APPLICATION_NAME,),
            fetch=True,
        )
        if not rows:
            logger.warning("No MAF configuration returned from sp_get_module_params")
            return result

        maf_json = _extract_maf_payload(rows[0])
        if not maf_json:
            logger.warning("MAF response payload was empty")
            return result

        site_configs = parse_maf_response(maf_json)
        result["loaded"] = True
        result["site_count"] = len(site_configs)

        if not site_configs:
            logger.warning("MAF response parsed but no site configurations found")
            return result

        logger.info("MAF configuration loaded for %d site(s)", len(site_configs))
        for site_id, site_config in sorted(site_configs.items(), key=lambda item: str(item[0])):
            _log_maf_site(_normalize_site_id(site_id), site_config)

    except Exception as exc:
        result["error"] = str(exc)
        logger.error("Failed to load MAF configuration at startup: %s", exc)

    return result


def run_startup_checks(*, check_maf: bool = True) -> Dict[str, Any]:
    """Run all startup configuration checks."""
    logger.info("Running startup configuration checks")
    summary: Dict[str, Any] = {"env": check_env_configuration()}

    if check_maf:
        db_configured = all(
            _env_is_set(os.getenv(name))
            for name in ("psgrsql_db_user", "psgrsql_db_pswd", "psgrsql_db_name", "psgrsql_db_host")
        )
        if db_configured:
            summary["maf"] = check_maf_configuration()
        else:
            logger.warning("Skipping MAF check: database env vars are not fully configured")
            summary["maf"] = {"loaded": False, "skipped": True}

    logger.info("Startup configuration checks complete")
    return summary
