-- Recomputes mart.mart_od_delay_by_slot in full from the star schema.
-- Run after 10_load_core.sql. Same logic as the local procedure
-- reporting.refresh_od_delay() (db/init/03_refresh_procedure.sql):
--
--   * every ordered pair of stops (A before B) of the same trip on the same
--     operating day, A must have a planned departure
--   * weekday and slot from the planned departure at A (not the operating day)
--   * median departure delay at A, median arrival delay at B, exact
--     (PERCENTILE_CONT ignores NULLs), converted to minutes and rounded once to
--     0.1, half away from zero (ROUND on NUMERIC)
--   * groups without any measurement are left out
--
-- DELETE + INSERT in one transaction: readers see the old content until the
-- commit, and on any error the old content stays.

BEGIN
  BEGIN TRANSACTION;

  DELETE FROM mart.mart_od_delay_by_slot WHERE TRUE;

  INSERT INTO mart.mart_od_delay_by_slot (
    line_key, linien_text, betreiber_abk,
    from_stop_key, from_stop, to_stop_key, to_stop,
    weekday, slot_hour,
    median_dep_delay_min, median_arr_delay_min,
    n_dep_obs, n_arr_obs)
  WITH pairs AS (
    SELECT
      a.line_key,
      a.stop_key                                       AS from_stop_key,
      b.stop_key                                       AS to_stop_key,
      MOD(EXTRACT(DAYOFWEEK FROM a.sched_dep) + 5, 7) + 1 AS weekday,   -- ISO, 1 = Monday
      a.dep_slot_hour                                  AS slot_hour,
      a.dep_delay_s,
      b.arr_delay_s
    FROM core.fact_stop_event AS a
    JOIN core.fact_stop_event AS b
      ON  b.betriebstag      = a.betriebstag
      AND b.fahrt_bezeichner = a.fahrt_bezeichner
      AND b.stop_sequence    > a.stop_sequence
    WHERE a.sched_dep IS NOT NULL
  ),
  with_median AS (
    -- PERCENTILE_CONT is an analytic function in BigQuery: compute it per row
    -- over the group, then collapse the group below.
    SELECT
      *,
      PERCENTILE_CONT(dep_delay_s, 0.5)
        OVER (PARTITION BY line_key, from_stop_key, to_stop_key, weekday, slot_hour) AS med_dep_s,
      PERCENTILE_CONT(arr_delay_s, 0.5)
        OVER (PARTITION BY line_key, from_stop_key, to_stop_key, weekday, slot_hour) AS med_arr_s
    FROM pairs
  ),
  agg AS (
    SELECT
      line_key, from_stop_key, to_stop_key, weekday, slot_hour,
      -- the median of integers is exact in FLOAT64 (a value or the mean of two)
      ROUND(CAST(MAX(med_dep_s) AS NUMERIC) / 60, 1) AS median_dep_delay_min,
      ROUND(CAST(MAX(med_arr_s) AS NUMERIC) / 60, 1) AS median_arr_delay_min,
      COUNT(dep_delay_s)                             AS n_dep_obs,
      COUNT(arr_delay_s)                             AS n_arr_obs
    FROM with_median
    GROUP BY line_key, from_stop_key, to_stop_key, weekday, slot_hour
    HAVING COUNT(dep_delay_s) > 0 OR COUNT(arr_delay_s) > 0
  )
  SELECT
    agg.line_key, l.linien_text, l.betreiber_abk,
    agg.from_stop_key, f.haltestellen_name,
    agg.to_stop_key,   t.haltestellen_name,
    agg.weekday, agg.slot_hour,
    agg.median_dep_delay_min, agg.median_arr_delay_min,
    agg.n_dep_obs, agg.n_arr_obs
  FROM agg
  JOIN core.dim_line AS l ON l.line_key = agg.line_key
  JOIN core.dim_stop AS f ON f.stop_key = agg.from_stop_key
  JOIN core.dim_stop AS t ON t.stop_key = agg.to_stop_key;

  COMMIT TRANSACTION;
EXCEPTION WHEN ERROR THEN
  ROLLBACK TRANSACTION;
  RAISE USING MESSAGE = FORMAT('20_refresh_mart.sql failed, mart unchanged: %s', @@error.message);
END;

SELECT
  COUNT(*)                                              AS rows_written,
  COUNT(DISTINCT FORMAT('%d-%d', from_stop_key, to_stop_key)) AS distinct_pairs
FROM mart.mart_od_delay_by_slot;
