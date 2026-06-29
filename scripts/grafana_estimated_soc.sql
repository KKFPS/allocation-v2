-- Grafana PostgreSQL time-series: estimated SOC for a single vehicle from charge schedule + routes.
--
-- Variables: $schedule_id, $vehicle_id, $site_id
-- Panel format: Time series (time, value)
--
-- Row 1 is the last known VSM reading immediately before the schedule starts.
-- Subsequent rows are estimated SOC at the end of each 30-min charge slot.

WITH vehicle AS (
  SELECT
    v.vehicle_id,
    v.battery_capacity,
    COALESCE(v.roll_kwh_mile, v.efficiency_kwh_mile) AS efficiency_kwh_mile
  FROM public.t_vehicle v
  WHERE v.vehicle_id::text = '$vehicle_id'
),

schedule_bounds AS (
  SELECT
    MIN(cs.charge_start_date_time) AS schedule_start,
    MAX(cs.charge_start_date_time) + INTERVAL '30 minutes' AS schedule_end
  FROM public.t_charge_schedule cs
  WHERE cs.schedule_id = $schedule_id
    AND cs.vehicle_id::text = '$vehicle_id'
),

initial_soc AS (
  SELECT
    vsm.estimated_soc AS soc_percent,
    vsm.date_time       AS soc_time
  FROM public.t_vsm vsm
  CROSS JOIN schedule_bounds sb
  WHERE vsm.vehicle_id::text = '$vehicle_id'
    AND sb.schedule_start IS NOT NULL
    AND vsm.date_time < sb.schedule_start
  ORDER BY vsm.date_time DESC
  LIMIT 1
),

charge_slots AS (
  SELECT
    cs.charge_start_date_time                         AS slot_start,
    cs.charge_start_date_time + INTERVAL '30 minutes' AS slot_end,
    COALESCE(cs.charge_power, 0) * 0.5                AS charge_kwh
  FROM public.t_charge_schedule cs
  WHERE cs.schedule_id = $schedule_id
    AND cs.vehicle_id::text = '$vehicle_id'
),

routes AS (
  SELECT
    rp.plan_start_date_time AS route_start,
    COALESCE(rp.plan_end_date_time, NOW()) AS route_end,
    rp.plan_mileage,
    GREATEST(
      EXTRACT(EPOCH FROM (
        COALESCE(rp.plan_end_date_time, NOW()) - rp.plan_start_date_time
      )) / 60.0,
      1.0
    ) AS route_duration_min
  FROM public.t_route_plan rp
  INNER JOIN public.t_route_allocated ra
    ON rp.route_id = ra.route_id
  CROSS JOIN schedule_bounds sb
  WHERE ra.vehicle_id_allocated::text = '$vehicle_id'
    AND rp.site_id = $site_id
    AND rp.route_status IN ('N', 'A')
    AND sb.schedule_start IS NOT NULL
    AND rp.plan_start_date_time < sb.schedule_end
    AND COALESCE(rp.plan_end_date_time, NOW()) > sb.schedule_start
),

slot_deltas AS (
  SELECT
    cs.slot_start,
    cs.slot_end,
    cs.charge_kwh
    - COALESCE((
        SELECT SUM(
          -- plan_mileage is in KM; efficiency is kWh/mile
          (r.plan_mileage / 1.60934) * veh.efficiency_kwh_mile
          * GREATEST(
              0,
              LEAST(
                EXTRACT(EPOCH FROM (
                  LEAST(cs.slot_end, r.route_end)
                  - GREATEST(cs.slot_start, r.route_start)
                )) / 60.0,
                30.0
              )
            )
          / r.route_duration_min
        )
        FROM routes r
        WHERE cs.slot_start < r.route_end
          AND cs.slot_end   > r.route_start
      ), 0) AS delta_kwh
  FROM charge_slots cs
  CROSS JOIN vehicle veh
),

-- Apply schedule deltas from the first charge slot onward
slots_after_vsm AS (
  SELECT sd.*
  FROM slot_deltas sd
  CROSS JOIN schedule_bounds sb
  WHERE sb.schedule_start IS NOT NULL
    AND sd.slot_start >= sb.schedule_start
),

running_soc AS (
  SELECT
    sd.slot_end AS time,
    ROUND(
      (
        GREATEST(
          0,
          LEAST(
            veh.battery_capacity,
            (init.soc_percent / 100.0) * veh.battery_capacity
            + SUM(sd.delta_kwh) OVER (ORDER BY sd.slot_start ROWS UNBOUNDED PRECEDING)
          )
        )
        / NULLIF(veh.battery_capacity, 0)
        * 100
      )::numeric,
      2
    ) AS value
  FROM slots_after_vsm sd
  CROSS JOIN vehicle veh
  CROSS JOIN initial_soc init
),

vsm_anchor AS (
  SELECT
    init.soc_time AS time,
    ROUND(init.soc_percent::numeric, 2) AS value
  FROM initial_soc init
)

SELECT time, value
FROM vsm_anchor
UNION ALL
SELECT rs.time, rs.value
FROM running_soc rs
WHERE $__timeFilter(rs.time)
ORDER BY time ASC;
