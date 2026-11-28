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
--
-- Blocking data-quality checks (docs/data_quality.md): before the commit, the
-- new content is checked; a failed check rolls back and the previous mart
-- stays published.

DECLARE failed_checks ARRAY<STRING>;

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

  SET failed_checks = ARRAY(
    SELECT FORMAT('%s (%d)', check_name, failing)
    FROM (
      SELECT 'duplicate_mart_key' AS check_name, COUNT(*) AS failing
      FROM (SELECT 1 FROM mart.mart_od_delay_by_slot
            GROUP BY line_key, from_stop_key, to_stop_key, weekday, slot_hour
            HAVING COUNT(*) > 1)
      UNION ALL
      -- a median exists exactly when there are measurements behind it
      SELECT 'median_does_not_match_count', COUNTIF(
          (median_dep_delay_min IS NULL) != (n_dep_obs = 0)
          OR (median_arr_delay_min IS NULL) != (n_arr_obs = 0))
      FROM mart.mart_od_delay_by_slot
      UNION ALL
      SELECT 'group_without_measurement', COUNTIF(n_dep_obs = 0 AND n_arr_obs = 0)
      FROM mart.mart_od_delay_by_slot
      UNION ALL
      SELECT 'weekday_or_slot_out_of_range', COUNTIF(
          weekday NOT BETWEEN 1 AND 7 OR slot_hour NOT BETWEEN 0 AND 23)
      FROM mart.mart_od_delay_by_slot
      UNION ALL
      SELECT 'origin_equals_destination', COUNTIF(from_stop_key = to_stop_key)
      FROM mart.mart_od_delay_by_slot
      UNION ALL
      -- facts with measurements, but an empty mart: the refresh lost everything
      SELECT 'mart_empty', IF(
          EXISTS (SELECT 1 FROM core.fact_stop_event WHERE dep_delay_s IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM mart.mart_od_delay_by_slot), 1, 0)
    )
    WHERE failing > 0
    ORDER BY check_name
  );
  IF ARRAY_LENGTH(failed_checks) > 0 THEN
    RAISE USING MESSAGE = FORMAT('failed checks on the new mart content: %s',
                                 ARRAY_TO_STRING(failed_checks, ', '));
  END IF;

  COMMIT TRANSACTION;
EXCEPTION WHEN ERROR THEN
  ROLLBACK TRANSACTION;
  RAISE USING MESSAGE = FORMAT('20_refresh_mart.sql failed, mart unchanged: %s', @@error.message);
END;

SELECT
  COUNT(*)                                              AS rows_written,
  COUNT(DISTINCT FORMAT('%d-%d', from_stop_key, to_stop_key)) AS distinct_pairs
FROM mart.mart_od_delay_by_slot;
