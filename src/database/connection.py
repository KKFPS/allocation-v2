"""Database connection management."""
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager
from src.config import DB_CONFIG
from src.utils.logging_config import logger


class DatabaseConnection:
    """Manages PostgreSQL database connections."""
    
    def __init__(self):
        """Initialize database connection configuration."""
        self.config = DB_CONFIG
        self._connection = None
    
    def connect(self):
        """Establish database connection."""
        try:
            self._connection = psycopg2.connect(
                user=self.config['user'],
                password=self.config['password'],
                database=self.config['database'],
                host=self.config['host'],
                port=self.config['port']
            )
            logger.info("Database connection established successfully")
            return self._connection
        except psycopg2.Error as e:
            logger.error(f"Database connection failed: {e}")
            raise
    
    def close(self):
        """Close database connection."""
        if self._connection:
            self._connection.close()
            logger.info("Database connection closed")
    
    @contextmanager
    def get_cursor(self, dict_cursor=True):
        """
        Context manager for database cursor.
        
        Args:
            dict_cursor: If True, return results as dictionaries
        
        Yields:
            Database cursor
        """
        conn = self._connection or self.connect()
        cursor_factory = RealDictCursor if dict_cursor else None
        cursor = conn.cursor(cursor_factory=cursor_factory)
        
        try:
            yield cursor
            conn.commit()
        except psycopg2.Error as e:
            conn.rollback()
            logger.error(f"Database error: {e}")
            raise
        finally:
            cursor.close()
    
    def execute_query(self, query, params=None, fetch=True):
        """
        Execute a query and return results.
        
        Args:
            query: SQL query string
            params: Query parameters
            fetch: Whether to fetch results
        
        Returns:
            Query results if fetch=True, otherwise None
        """
        with self.get_cursor() as cursor:
            cursor.execute(query, params)
            if fetch:
                return cursor.fetchall()
            return None
    
    def execute_many(self, query, params_list):
        """
        Execute a query with multiple parameter sets.
        
        Args:
            query: SQL query string
            params_list: List of parameter tuples
        """
        with self.get_cursor() as cursor:
            cursor.executemany(query, params_list)
    
    def call_stored_procedure(self, proc_name, params=None):
        """
        Call a stored procedure.
        
        Args:
            proc_name: Stored procedure name
            params: Procedure parameters
        
        Returns:
            Procedure results
        """
        with self.get_cursor() as cursor:
            cursor.callproc(proc_name, params or [])
            try:
                return cursor.fetchall()
            except psycopg2.ProgrammingError:
                # No results to fetch
                return None
    
    def get_vehicle_chargers_in_window(self, vehicle_ids, reference_time):
        """
        Get most recent charger for each vehicle within 18-hour window before reference time.
        At most one vehicle per charger: if multiple vehicles used the same charger,
        only the one with the latest start_date_time keeps it; others get None.
        
        Args:
            vehicle_ids: List of vehicle IDs
            reference_time: Reference datetime (or None for current time)
        
        Returns:
            Dict mapping every vehicle_id -> charger_id or None. Size equals len(vehicle_ids).
        """
        from src.database.queries import Queries
        from datetime import datetime
        
        if not vehicle_ids:
            return {}
        
        # Use reference time or current time
        ref_time = reference_time or datetime.now()
        
        query = Queries.GET_VEHICLE_CHARGERS_IN_WINDOW
        # Pass vehicle_ids as a list for ANY() operator, and ref_time twice
        results = self.execute_query(query, (vehicle_ids, ref_time, ref_time))
        
        # One entry per vehicle; vehicles with no charge in window or that lost charger get None
        charger_map = {vid: None for vid in vehicle_ids}
        
        if results:
            # One vehicle per charger: for each charger keep only the vehicle with latest start_date_time
            charger_to_best = {}  # charger_id -> (vehicle_id, start_date_time)
            for row in results:
                vid, cid, start_dt = row['vehicle_id'], row['charger_id'], row['start_date_time']
                if cid not in charger_to_best or start_dt > charger_to_best[cid][1]:
                    charger_to_best[cid] = (vid, start_dt)
            for row in results:
                vid, cid = row['vehicle_id'], row['charger_id']
                if charger_to_best[cid][0] == vid:
                    charger_map[vid] = cid

        assigned = sum(1 for v in charger_map.values() if v is not None)
        logger.info(f"Retrieved chargers for {assigned}/{len(vehicle_ids)} vehicles "
                   f"(reference time: {ref_time})")

        return charger_map


# Global database connection instance
db = DatabaseConnection()
