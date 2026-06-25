"""
Clone allocation/scheduling data from a source PostgreSQL DB to a destination DB
for a configurable time window (default 1 hour for VSM, forecast, price data).

t_route_plan is upserted for all rows with plan_start_date_time >= now for each
site (no upper time bound). Allocation tables (t_allocation_monitor,
t_route_allocated, t_route_allocated_history) are not cloned.

Filters mirror src/database/queries.py: site_id, client_id, vehicle_id, and
time-bounded forecast / price / VSM / charge data.

Environment variables (prefix SOURCE_ / DEST_ for each connection field):
  psgrsql_db_host, psgrsql_db_user, psgrsql_db_pswd, psgrsql_db_name, psgrsql_db_port

DEST falls back to unprefixed psgrsql_db_* when DEST_* is not set.

CLI entry point: scripts/clone_db_window.py
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor, execute_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

from src.utils.logging_config import logger  # noqa: E402

# Lookback used by GET_VEHICLE_CHARGERS_IN_WINDOW and VSM "as of" queries
CHARGER_VSM_LOOKBACK_HOURS = 18
FORECAST_METHOD_ID = 2


@dataclass
class DbConfig:
    host: str
    user: str
    password: str
    database: str
    port: str = "5432"

    @classmethod
    def from_env(cls, prefix: str) -> "DbConfig":
        def get(key: str) -> str:
            value = os.getenv(f"{prefix}{key}")
            if value is None:
                value = os.getenv(key)
            if value is None:
                raise ValueError(f"Missing database env var: {prefix}{key} or {key}")
            return value

        port = os.getenv(f"{prefix}psgrsql_db_port") or os.getenv("psgrsql_db_port", "5432")

        return cls(
            host=get("psgrsql_db_host"),
            user=get("psgrsql_db_user"),
            password=get("psgrsql_db_pswd"),
            database=get("psgrsql_db_name"),
            port=port,
        )


@dataclass
class CloneFilters:
    site_ids: List[int]
    client_id: Optional[int]
    vehicle_ids: Optional[List[int]]
    window_start: datetime
    window_end: datetime
    lookback_start: datetime
    route_plan_from: datetime
    include_scheduler: bool = True

    @property
    def vehicle_filter_sql(self) -> str:
        if self.vehicle_ids:
            return "AND v.vehicle_id = ANY(%(vehicle_ids)s)"
        return ""


@dataclass
class CloneStats:
    tables: Dict[str, int] = field(default_factory=dict)

    def add(self, table: str, count: int) -> None:
        self.tables[table] = count


class DbCloneClient:
    def __init__(self, config: DbConfig, label: str):
        self.config = config
        self.label = label
        self._conn = None

    def connect(self):
        self._conn = psycopg2.connect(
            user=self.config.user,
            password=self.config.password,
            database=self.config.database,
            host=self.config.host,
            port=self.config.port,
        )
        logger.info("Connected to %s DB: %s@%s/%s", self.label, self.config.user, self.config.host, self.config.database)

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def fetch_all(self, query: str, params: Optional[dict] = None) -> List[dict]:
        with self._conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params or {})
            return list(cur.fetchall())

    def execute(self, query: str, params: Optional[Sequence[Any]] = None) -> int:
        with self._conn.cursor() as cur:
            cur.execute(query, params)
            return cur.rowcount

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()


def resolve_site_ids(
    source: DbCloneClient,
    client_id: Optional[int],
    site_id: Optional[int],
    site_ids: Optional[List[int]] = None,
) -> List[int]:
    if site_ids is not None:
        rows = source.fetch_all(
            """
            SELECT site_id, client_id
            FROM t_site
            WHERE site_id = ANY(%(site_ids)s)
              AND COALESCE(is_deleted, false) = false
            ORDER BY site_id
            """,
            {"site_ids": site_ids},
        )
        found = {row["site_id"] for row in rows}
        missing = sorted(set(site_ids) - found)
        if missing:
            raise ValueError(f"Sites not found in source DB: {missing}")
        if client_id is not None:
            mismatched = [
                row["site_id"]
                for row in rows
                if row["client_id"] != client_id
            ]
            if mismatched:
                raise ValueError(
                    f"Sites {mismatched} do not belong to client_id={client_id}"
                )
        return [row["site_id"] for row in rows]

    if site_id is not None:
        query = """
            SELECT site_id, client_id
            FROM t_site
            WHERE site_id = %(site_id)s
              AND COALESCE(is_deleted, false) = false
        """
        rows = source.fetch_all(query, {"site_id": site_id})
        if not rows:
            raise ValueError(f"Site {site_id} not found in source DB")
        if client_id is not None and rows[0]["client_id"] != client_id:
            raise ValueError(
                f"Site {site_id} belongs to client {rows[0]['client_id']}, not {client_id}"
            )
        return [site_id]

    if client_id is None:
        raise ValueError("Provide --site-id, --site-ids, and/or --client-id")

    rows = source.fetch_all(
        """
        SELECT site_id
        FROM t_site
        WHERE client_id = %(client_id)s
          AND COALESCE(is_deleted, false) = false
        ORDER BY site_id
        """,
        {"client_id": client_id},
    )
    if not rows:
        raise ValueError(f"No sites found for client_id={client_id}")
    return [row["site_id"] for row in rows]


def base_params(filters: CloneFilters) -> dict:
    return {
        "site_ids": filters.site_ids,
        "client_id": filters.client_id,
        "vehicle_ids": filters.vehicle_ids,
        "window_start": filters.window_start,
        "window_end": filters.window_end,
        "lookback_start": filters.lookback_start,
        "route_plan_from": filters.route_plan_from,
        "forecast_method_id": FORECAST_METHOD_ID,
    }


def build_select_queries(filters: CloneFilters) -> List[Tuple[str, str]]:
    """Return (table_name, select_sql) pairs in dependency order."""
    vf = filters.vehicle_filter_sql
    queries: List[Tuple[str, str]] = []

    client_site_filter = ""
    if filters.client_id is not None:
        client_site_filter = "AND s.client_id = %(client_id)s"

    queries.append(
        (
            "t_site",
            f"""
            SELECT s.*
            FROM t_site s
            WHERE s.site_id = ANY(%(site_ids)s)
              {client_site_filter}
              AND COALESCE(s.is_deleted, false) = false
            """,
        )
    )

    client_vehicle_filter = ""
    if filters.client_id is not None:
        client_vehicle_filter = "AND v.client_id = %(client_id)s"

    queries.append(
        (
            "t_vehicle",
            f"""
            SELECT v.*
            FROM t_vehicle v
            WHERE v.site_id = ANY(%(site_ids)s)
              {client_vehicle_filter}
              {vf}
              AND COALESCE(v.is_deleted, false) = false
            """,
        )
    )

    queries.append(
        (
            "t_vehicle_telematics",
            f"""
            SELECT vt.*
            FROM t_vehicle_telematics vt
            INNER JOIN t_vehicle v ON v.vehicle_id = vt.vehicle_id
            WHERE v.site_id = ANY(%(site_ids)s)
              {vf}
            """,
        )
    )

    queries.append(
        (
            "t_charger",
            """
            SELECT c.*
            FROM t_charger c
            WHERE c.site_id = ANY(%(site_ids)s)
              AND COALESCE(c.active, true) = true
              AND COALESCE(c.is_deleted, false) = false
            """,
        )
    )

    queries.append(
        (
            "t_route_plan",
            """
            SELECT rp.*
            FROM t_route_plan rp
            WHERE rp.site_id = ANY(%(site_ids)s)
              AND rp.plan_start_date_time >= %(route_plan_from)s
            """,
        )
    )

    queries.append(
        (
            "t_vsm",
            f"""
            SELECT vsm.*
            FROM t_vsm vsm
            INNER JOIN t_vehicle v ON v.vehicle_id = vsm.vehicle_id
            WHERE v.site_id = ANY(%(site_ids)s)
              {vf}
              AND vsm.date_time >= %(lookback_start)s
              AND vsm.date_time <= %(window_end)s
            """,
        )
    )

    queries.append(
        (
            "t_vehicle_charge",
            f"""
            SELECT vc.*
            FROM t_vehicle_charge vc
            INNER JOIN t_vehicle v ON v.vehicle_id = vc.vehicle_id
            WHERE v.site_id = ANY(%(site_ids)s)
              {vf}
              AND vc.start_date_time >= %(lookback_start)s
              AND vc.start_date_time <= %(window_end)s
            """,
        )
    )

    queries.append(
        (
            "t_site_energy_forecast_history",
            """
            SELECT sefh.*
            FROM t_site_energy_forecast_history sefh
            WHERE sefh.site_id = ANY(%(site_ids)s)
              AND sefh.forecasted_date_time >= %(window_start)s
              AND sefh.forecasted_date_time <= %(window_end)s
              AND sefh.forecasting_method_id = %(forecast_method_id)s
            """,
        )
    )

    price_client_filter = ""
    if filters.client_id is not None:
        price_client_filter = "AND (mep.client_id = %(client_id)s OR mep.client_id IS NULL)"

    queries.append(
        (
            "t_multisite_electricity_price",
            f"""
            SELECT mep.*
            FROM t_multisite_electricity_price mep
            WHERE mep.date_time >= %(window_start)s
              AND mep.date_time <= %(window_end)s
              {price_client_filter}
            """,
        )
    )

    if filters.include_scheduler:
        pass
    
        # queries.append(
        #     (
        #         "t_scheduler",
        #         """
        #         SELECT sch.*
        #         FROM t_scheduler sch
        #         WHERE sch.device_id = ANY(%(site_ids)s)
        #           AND sch.created_datetime >= %(window_start)s
        #           AND sch.created_datetime <= %(window_end)s
        #         """,
        #     )
        # )

        # queries.append(
        #     (
        #         "t_charge_schedule",
        #         f"""
        #         SELECT cs.*
        #         FROM t_charge_schedule cs
        #         INNER JOIN t_vehicle v ON v.vehicle_id = cs.vehicle_id
        #         WHERE v.site_id = ANY(%(site_ids)s)
        #           {vf}
        #           AND cs.charge_start_date_time >= %(window_start)s
        #           AND cs.charge_start_date_time <= %(window_end)s
        #         """,
        #     )
        # )

    return queries


# Primary key columns per table (for ON CONFLICT upsert)
def _quote_col(col: str) -> str:
    """Quote identifiers so reserved words (ASC, VOR, etc.) are valid SQL."""
    return f'"{col}"'


TABLE_PRIMARY_KEYS: Dict[str, List[str]] = {
    "t_site": ["site_id"],
    "t_vehicle": ["vehicle_id"],
    "t_vehicle_telematics": ["telematic_id", "vehicle_id"],
    "t_charger": ["charger_id"],
    "t_route_plan": ["route_id"],
    "t_vsm": ["vehicle_id", "date_time"],
    "t_vehicle_charge": ["charger_id", "vehicle_id", "start_date_time"],
    "t_site_energy_forecast_history": ["id"],
    "t_scheduler": ["schedule_id"],
    "t_charge_schedule": ["schedule_id", "vehicle_id", "charge_start_date_time"],
}


def upsert_rows(
    dest: DbCloneClient,
    table: str,
    rows: List[dict],
    dry_run: bool,
) -> int:
    if not rows:
        return 0

    columns = list(rows[0].keys())
    pk_cols = TABLE_PRIMARY_KEYS.get(table)
    if not pk_cols:
        raise ValueError(f"No primary key mapping for table {table}")

    placeholders = ", ".join(["%s"] * len(columns))
    col_list = ", ".join(_quote_col(c) for c in columns)
    update_cols = [c for c in columns if c not in pk_cols]
    conflict_target = ", ".join(_quote_col(c) for c in pk_cols)

    if update_cols:
        set_clause = ", ".join(
            f"{_quote_col(c)} = EXCLUDED.{_quote_col(c)}" for c in update_cols
        )
        sql = f"""
            INSERT INTO {table} ({col_list})
            VALUES %s
            ON CONFLICT ({conflict_target}) DO UPDATE SET {set_clause}
        """
    else:
        sql = f"""
            INSERT INTO {table} ({col_list})
            VALUES %s
            ON CONFLICT ({conflict_target}) DO NOTHING
        """

    values = [tuple(row[c] for c in columns) for row in rows]

    if dry_run:
        logger.info("[dry-run] Would upsert %s rows into %s", len(values), table)
        return len(values)

    with dest._conn.cursor() as cur:
        execute_values(cur, sql, values, template=f"({placeholders})")
    return len(values)


def upsert_multisite_prices(
    dest: DbCloneClient,
    rows: List[dict],
    filters: CloneFilters,
    dry_run: bool,
) -> int:
    """t_multisite_electricity_price has no PK — delete window slice then insert."""
    if not rows:
        return 0

    if dry_run:
        logger.info("[dry-run] Would replace %s rows in t_multisite_electricity_price", len(rows))
        return len(rows)

    client_filter = ""
    params: List[Any] = [filters.window_start, filters.window_end]
    if filters.client_id is not None:
        client_filter = "AND (client_id = %s OR client_id IS NULL)"
        params.append(filters.client_id)

    dest.execute(
        f"""
        DELETE FROM t_multisite_electricity_price
        WHERE date_time >= %s AND date_time <= %s
        {client_filter}
        """,
        params,
    )

    columns = list(rows[0].keys())
    col_list = ", ".join(_quote_col(c) for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"INSERT INTO t_multisite_electricity_price ({col_list}) VALUES %s"
    values = [tuple(row[c] for c in columns) for row in rows]

    with dest._conn.cursor() as cur:
        execute_values(cur, sql, values, template=f"({placeholders})")
    return len(values)


def clone_data(
    source: DbCloneClient,
    dest: DbCloneClient,
    filters: CloneFilters,
    dry_run: bool,
) -> CloneStats:
    stats = CloneStats()
    params = base_params(filters)
    select_queries = build_select_queries(filters)

    for table, select_sql in select_queries:
        rows = source.fetch_all(select_sql, params)
        logger.info("Fetched %s rows from source.%s", len(rows), table)

        if table == "t_multisite_electricity_price":
            count = upsert_multisite_prices(dest, rows, filters, dry_run)
        else:
            count = upsert_rows(dest, table, rows, dry_run)

        stats.add(table, count)
        if not dry_run:
            dest.commit()

    return stats


def parse_vehicle_ids(raw: Optional[str]) -> Optional[List[int]]:
    if not raw:
        return None
    return [int(v.strip()) for v in raw.split(",") if v.strip()]


def parse_site_ids(raw: Optional[str]) -> Optional[List[int]]:
    if not raw:
        return None
    return [int(v.strip()) for v in raw.split(",") if v.strip()]


def previous_utc_hour_window(
    at: Optional[datetime] = None,
) -> Tuple[datetime, datetime]:
    """Return the previous complete UTC hour as naive UTC datetimes."""
    if at is None:
        at_utc = datetime.now(timezone.utc)
    elif at.tzinfo is None:
        at_utc = at.replace(tzinfo=timezone.utc)
    else:
        at_utc = at.astimezone(timezone.utc)

    hour_start = at_utc.replace(minute=0, second=0, microsecond=0)
    window_end = hour_start
    window_start = window_end - timedelta(hours=1)
    return (
        window_start.replace(tzinfo=None),
        window_end.replace(tzinfo=None),
    )


def run_clone(
    site_ids: List[int],
    *,
    client_id: Optional[int] = None,
    vehicle_ids: Optional[List[int]] = None,
    window_start: datetime,
    window_hours: float = 1.0,
    include_scheduler: bool = True,
    dry_run: bool = False,
) -> CloneStats:
    """Clone source → destination for the given sites and time window."""
    window_end = window_start + timedelta(hours=window_hours)
    lookback_start = window_start - timedelta(hours=CHARGER_VSM_LOOKBACK_HOURS)
    route_plan_from = datetime.now().replace(microsecond=0)

    source = DbCloneClient(DbConfig.from_env("SOURCE_"), "source")
    dest = DbCloneClient(DbConfig.from_env("DEST_"), "destination")

    try:
        source.connect()
        if not dry_run:
            dest.connect()

        resolved_site_ids = resolve_site_ids(
            source,
            client_id=client_id,
            site_id=None,
            site_ids=site_ids,
        )
        filters = CloneFilters(
            site_ids=resolved_site_ids,
            client_id=client_id,
            vehicle_ids=vehicle_ids,
            window_start=window_start,
            window_end=window_end,
            lookback_start=lookback_start,
            route_plan_from=route_plan_from,
            include_scheduler=include_scheduler,
        )

        logger.info("=" * 60)
        logger.info("DB CLONE — %sh window", window_hours)
        logger.info("Sites:         %s", resolved_site_ids)
        logger.info("Client ID:     %s", client_id)
        logger.info("Vehicle IDs:   %s", vehicle_ids or "all")
        logger.info("Window:        %s → %s", window_start, window_end)
        logger.info("Route plan from: %s (no upper bound)", route_plan_from)
        logger.info("VSM/charge lookback from: %s", lookback_start)
        logger.info("Dry run:       %s", dry_run)
        logger.info("=" * 60)

        return clone_data(source, dest, filters, dry_run=dry_run)
    except Exception:
        if dest._conn:
            dest.rollback()
        raise
    finally:
        source.close()
        dest.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clone allocation/scheduling DB data for a time window",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--site-id", type=int, help="Single site ID to clone")
    parser.add_argument(
        "--site-ids",
        type=str,
        help="Comma-separated site IDs to clone (alternative to --site-id)",
    )
    parser.add_argument("--client-id", type=int, help="Client ID filter (validates site ownership)")
    parser.add_argument(
        "--vehicle-ids",
        type=str,
        help="Comma-separated vehicle IDs to restrict clone (default: all site vehicles)",
    )
    parser.add_argument(
        "--window-start",
        type=str,
        help="Window start (YYYY-MM-DD HH:MM:SS). Default: now",
    )
    parser.add_argument(
        "--window-hours",
        type=float,
        default=24.0,
        help="Window length in hours (default: 24)",
    )
    parser.add_argument(
        "--no-scheduler",
        action="store_true",
        help="Skip t_scheduler and t_charge_schedule",
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch counts only; do not write to destination")
    return parser.parse_args()


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_args()

    if args.window_start:
        window_start = datetime.strptime(args.window_start, "%Y-%m-%d %H:%M:%S")
    else:
        window_start = datetime.now().replace(microsecond=0)

    site_ids_arg = parse_site_ids(args.site_ids)
    if site_ids_arg is None and args.site_id is not None:
        site_ids_arg = [args.site_id]

    try:
        if site_ids_arg is not None:
            stats = run_clone(
                site_ids_arg,
                client_id=args.client_id,
                vehicle_ids=parse_vehicle_ids(args.vehicle_ids),
                window_start=window_start,
                window_hours=args.window_hours,
                include_scheduler=not args.no_scheduler,
                dry_run=args.dry_run,
            )
        else:
            source = DbCloneClient(DbConfig.from_env("SOURCE_"), "source")
            dest = DbCloneClient(DbConfig.from_env("DEST_"), "destination")
            try:
                source.connect()
                if not args.dry_run:
                    dest.connect()

                resolved_site_ids = resolve_site_ids(source, args.client_id, args.site_id)
                window_end = window_start + timedelta(hours=args.window_hours)
                lookback_start = window_start - timedelta(hours=CHARGER_VSM_LOOKBACK_HOURS)
                route_plan_from = datetime.now().replace(microsecond=0)
                filters = CloneFilters(
                    site_ids=resolved_site_ids,
                    client_id=args.client_id,
                    vehicle_ids=parse_vehicle_ids(args.vehicle_ids),
                    window_start=window_start,
                    window_end=window_end,
                    lookback_start=lookback_start,
                    route_plan_from=route_plan_from,
                    include_scheduler=not args.no_scheduler,
                )

                logger.info("=" * 60)
                logger.info("DB CLONE — %sh window", args.window_hours)
                logger.info("Sites:         %s", resolved_site_ids)
                logger.info("Client ID:     %s", args.client_id)
                logger.info("Vehicle IDs:   %s", filters.vehicle_ids or "all")
                logger.info("Window:        %s → %s", window_start, window_end)
                logger.info("Route plan from: %s (no upper bound)", route_plan_from)
                logger.info("VSM/charge lookback from: %s", lookback_start)
                logger.info("Dry run:       %s", args.dry_run)
                logger.info("=" * 60)

                stats = clone_data(source, dest, filters, dry_run=args.dry_run)
            except Exception:
                if dest._conn:
                    dest.rollback()
                raise
            finally:
                source.close()
                dest.close()

        logger.info("Clone complete:")
        for table, count in stats.tables.items():
            logger.info("  %-35s %6d rows", table, count)

        return 0
    except Exception as exc:
        logger.error("Clone failed: %s", exc, exc_info=True)
        return 1
