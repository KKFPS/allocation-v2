-- Rollback: Remove assigned_charger_power_kw from t_charge_schedule
-- Purpose: Revert changes from 001_add_charger_power_to_schedule.sql
-- Author: Allocation-v2
-- Date: 2024-01-26

BEGIN;

-- Drop index
DROP INDEX IF EXISTS idx_charge_schedule_charger_power;

-- Drop constraint
ALTER TABLE t_charge_schedule
DROP CONSTRAINT IF EXISTS chk_charger_power_range;

-- Drop column
ALTER TABLE t_charge_schedule 
DROP COLUMN IF EXISTS assigned_charger_power_kw;

COMMIT;
