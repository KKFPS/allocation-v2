-- Migration: Add assigned_charger_power_kw to t_charge_schedule
-- Purpose: Store the maximum power (kW) of the assigned charger/power class
-- Author: Allocation-v2
-- Date: 2024-01-26

BEGIN;

-- Add column for assigned charger power
ALTER TABLE t_charge_schedule 
ADD COLUMN IF NOT EXISTS assigned_charger_power_kw NUMERIC(10,2);

-- Add comment for documentation
COMMENT ON COLUMN t_charge_schedule.assigned_charger_power_kw 
IS 'Maximum power (kW) of the assigned charger/power class at time of scheduling';

-- Optional: Backfill existing records from t_charger
-- This populates historical data based on current charger configuration
UPDATE t_charge_schedule cs
SET assigned_charger_power_kw = c.max_power
FROM t_charger c
WHERE c.charger_id = CAST(cs.connector_id AS INTEGER)
  AND cs.assigned_charger_power_kw IS NULL
  AND cs.connector_id IS NOT NULL
  AND cs.connector_id != '';

-- Add constraint to ensure reasonable power values
ALTER TABLE t_charge_schedule
ADD CONSTRAINT chk_charger_power_range 
CHECK (assigned_charger_power_kw IS NULL OR 
       (assigned_charger_power_kw >= 0 AND assigned_charger_power_kw <= 350));

-- Optional: Add index for analytics queries
CREATE INDEX IF NOT EXISTS idx_charge_schedule_charger_power 
ON t_charge_schedule(assigned_charger_power_kw) 
WHERE assigned_charger_power_kw IS NOT NULL;

COMMIT;
