-- Loads the content of core.stg_stop_event into the star schema.
-- Run after every transform run (Kestra), before 20_refresh_mart.sql.
--
-- Input: core.stg_stop_event (rows) and core.stg_run_scope (processed days
-- and lines), loaded by Kestra from the transform's Parquet files in processed/.
--   1. dim_stop, dim_line: MERGE (insert new, update changed attributes)
--   2. fact_stop_event:    delete the rows of the processed days AND lines,
--                          insert them again from the load table
-- Rows of other lines on the same day are not touched.
-- All in one transaction: on any error nothing is changed.
--
-- Blocking data-quality checks (docs/data_quality.md): before anything is
-- written, the load table is checked; after the insert, referential integrity
-- of the new fact rows is checked inside the transaction. A failed check stops
-- the load with an error listing the failed checks, and nothing is changed.
--
-- Idempotent: running it twice with the same load table gives the same result.
-- SCD type 1 with a guard: attributes are only overwritten by data from the
-- same or a later operating day, so a backfill of old days never replaces a
-- newer stop name with an older one.

DECLARE run_days ARRAY<DATE> DEFAULT (
  SELECT ARRAY_AGG(DISTINCT betriebstag ORDER BY betriebstag)
  FROM core.stg_run_scope
);
DECLARE failed_checks ARRAY<STRING>;
DECLARE fact_orphans INT64;

IF run_days IS NULL THEN
  SELECT 'core.stg_run_scope is empty, nothing loaded' AS info;
  RETURN;
END IF;

-- ---------------------------------------------------------------------------
-- Blocking checks on the load table. Each returns the number of failing rows
-- (or trips, days); every check > 0 stops the load.
-- ---------------------------------------------------------------------------
SET failed_checks = ARRAY(
  SELECT FORMAT('%s (%d)', check_name, failing)
  FROM (
    -- the delete below only covers the processed days and lines
    SELECT 'row_outside_run_scope' AS check_name, COUNT(*) AS failing
    FROM core.stg_stop_event AS e
    LEFT JOIN core.stg_run_scope AS s USING (betriebstag, linien_text, betreiber_abk)
    WHERE s.betriebstag IS NULL
    UNION ALL
    -- the Parquet columns are NULLABLE: keys and flags must be filled
    SELECT 'required_value_missing', COUNTIF(
        betriebstag IS NULL OR fahrt_bezeichner IS NULL OR fahrt_bezeichner = ''
        OR bpuic IS NULL OR linien_text IS NULL OR betreiber_abk IS NULL
        OR stop_sequence IS NULL OR is_additional_trip IS NULL)
    FROM core.stg_stop_event
    UNION ALL
    -- BigQuery does not enforce primary keys
    SELECT 'duplicate_fact_key', COUNT(*)
    FROM (SELECT 1 FROM core.stg_stop_event
          GROUP BY betriebstag, fahrt_bezeichner, bpuic HAVING COUNT(*) > 1)
    UNION ALL
    -- the mart pairs stops by stop_sequence: it must be 1..n per trip
    SELECT 'stop_sequence_not_1_to_n', COUNT(*)
    FROM (SELECT 1 FROM core.stg_stop_event
          GROUP BY betriebstag, fahrt_bezeichner
          HAVING MIN(stop_sequence) != 1 OR MAX(stop_sequence) != COUNT(*)
              OR COUNT(DISTINCT stop_sequence) != COUNT(*))
    UNION ALL
    -- a trip belongs to exactly one line
    SELECT 'trip_with_several_lines', COUNT(*)
    FROM (SELECT 1 FROM core.stg_stop_event
          GROUP BY betriebstag, fahrt_bezeichner
          HAVING COUNT(DISTINCT FORMAT('%s|%s', linien_text, betreiber_abk)) > 1)
    UNION ALL
    -- planned times travel as text 'YYYY-MM-DD HH:MM:SS'
    SELECT 'planned_time_unreadable', COUNTIF(
        (sched_arr IS NOT NULL AND SAFE_CAST(sched_arr AS DATETIME) IS NULL)
        OR (sched_dep IS NOT NULL AND SAFE_CAST(sched_dep AS DATETIME) IS NULL))
    FROM core.stg_stop_event
    UNION ALL
    -- a delay needs the planned time it refers to
    SELECT 'delay_without_planned_time', COUNTIF(
        (arr_delay_s IS NOT NULL AND sched_arr IS NULL)
        OR (dep_delay_s IS NOT NULL AND sched_dep IS NULL))
    FROM core.stg_stop_event
    UNION ALL
    -- hard upper bound, independent of the transform's configurable limit
    SELECT 'delay_over_24h', COUNTIF(ABS(arr_delay_s) > 86400 OR ABS(dep_delay_s) > 86400)
    FROM core.stg_stop_event
    UNION ALL
    -- the calendar dimension must cover every loaded day
    SELECT 'day_not_in_dim_date', COUNT(*)
    FROM UNNEST(run_days) AS d
    LEFT JOIN core.dim_date AS c ON c.date = d
    WHERE c.date IS NULL
  )
  WHERE failing > 0
  ORDER BY check_name
);

IF ARRAY_LENGTH(failed_checks) > 0 THEN
  RAISE USING MESSAGE = FORMAT(
    'Load stopped, nothing changed. Failed checks on core.stg_stop_event: %s',
    ARRAY_TO_STRING(failed_checks, ', '));
END IF;

BEGIN
  BEGIN TRANSACTION;

  -- 1a. dim_stop: latest attributes per stop within the load table
  MERGE core.dim_stop AS t
  USING (
    SELECT
      core.stop_key(bpuic) AS stop_key,
      bpuic,
      ARRAY_AGG(STRUCT(haltestellen_name, betriebstag)
                ORDER BY betriebstag DESC, haltestellen_name LIMIT 1)[OFFSET(0)].*
    FROM core.stg_stop_event
    GROUP BY bpuic
  ) AS s
  ON t.stop_key = s.stop_key
  WHEN MATCHED AND s.betriebstag >= t.source_day
               AND t.haltestellen_name IS DISTINCT FROM s.haltestellen_name THEN
    UPDATE SET haltestellen_name = s.haltestellen_name,
               source_day        = s.betriebstag,
               updated_at        = CURRENT_TIMESTAMP()
  WHEN NOT MATCHED THEN
    INSERT (stop_key, bpuic, haltestellen_name, source_day, updated_at)
    VALUES (s.stop_key, s.bpuic, s.haltestellen_name, s.betriebstag, CURRENT_TIMESTAMP());

  -- 1b. dim_line: latest attributes per line within the load table
  MERGE core.dim_line AS t
  USING (
    SELECT
      core.line_key(linien_text, betreiber_abk) AS line_key,
      linien_text,
      betreiber_abk,
      ARRAY_AGG(STRUCT(betreiber_name, verkehrsmittel_text, produkt_id, betriebstag)
                ORDER BY betriebstag DESC, betreiber_name, verkehrsmittel_text, produkt_id
                LIMIT 1)[OFFSET(0)].*
    FROM core.stg_stop_event
    GROUP BY linien_text, betreiber_abk
  ) AS s
  ON t.line_key = s.line_key
  WHEN MATCHED AND s.betriebstag >= t.source_day
               AND (t.betreiber_name      IS DISTINCT FROM s.betreiber_name
                 OR t.verkehrsmittel_text IS DISTINCT FROM s.verkehrsmittel_text
                 OR t.produkt_id          IS DISTINCT FROM s.produkt_id) THEN
    UPDATE SET betreiber_name      = s.betreiber_name,
               verkehrsmittel_text = s.verkehrsmittel_text,
               produkt_id          = s.produkt_id,
               source_day          = s.betriebstag,
               updated_at          = CURRENT_TIMESTAMP()
  WHEN NOT MATCHED THEN
    INSERT (line_key, linien_text, betreiber_abk, betreiber_name, verkehrsmittel_text,
            produkt_id, source_day, updated_at)
    VALUES (s.line_key, s.linien_text, s.betreiber_abk, s.betreiber_name, s.verkehrsmittel_text,
            s.produkt_id, s.betriebstag, CURRENT_TIMESTAMP());

  -- 2. fact: replace the processed days and lines
  --    (the IN UNNEST condition lets BigQuery prune to these partitions)
  DELETE FROM core.fact_stop_event AS f
  WHERE f.betriebstag IN UNNEST(run_days)
    AND EXISTS (
      SELECT 1
      FROM core.stg_run_scope AS s
      WHERE s.betriebstag = f.betriebstag
        AND core.line_key(s.linien_text, s.betreiber_abk) = f.line_key);

  INSERT INTO core.fact_stop_event (
    betriebstag, fahrt_bezeichner, stop_key, line_key, stop_sequence,
    sched_arr, sched_dep, arr_slot_hour, dep_slot_hour,
    arr_delay_s, dep_delay_s, is_additional_trip, loaded_at)
  SELECT
    betriebstag,
    fahrt_bezeichner,
    core.stop_key(bpuic),
    core.line_key(linien_text, betreiber_abk),
    stop_sequence,
    -- text 'YYYY-MM-DD HH:MM:SS', Swiss local time (see transform/app/output.py)
    CAST(sched_arr AS DATETIME),
    CAST(sched_dep AS DATETIME),
    EXTRACT(HOUR FROM CAST(sched_arr AS DATETIME)),
    EXTRACT(HOUR FROM CAST(sched_dep AS DATETIME)),
    arr_delay_s,
    dep_delay_s,
    is_additional_trip,
    CURRENT_TIMESTAMP()
  FROM core.stg_stop_event;

  -- Referential integrity of the new fact rows (BigQuery does not enforce
  -- foreign keys). Checked inside the transaction: on failure the rollback
  -- below restores the previous state.
  SET fact_orphans = (
    SELECT COUNT(*)
    FROM core.fact_stop_event AS f
    LEFT JOIN core.dim_stop AS s ON s.stop_key = f.stop_key
    LEFT JOIN core.dim_line AS l ON l.line_key = f.line_key
    LEFT JOIN core.dim_time_slot AS ta ON ta.slot_hour = f.arr_slot_hour
    LEFT JOIN core.dim_time_slot AS td ON td.slot_hour = f.dep_slot_hour
    WHERE f.betriebstag IN UNNEST(run_days)
      AND (s.stop_key IS NULL OR l.line_key IS NULL
           OR (f.arr_slot_hour IS NOT NULL AND ta.slot_hour IS NULL)
           OR (f.dep_slot_hour IS NOT NULL AND td.slot_hour IS NULL)));
  IF fact_orphans > 0 THEN
    RAISE USING MESSAGE = FORMAT(
      'referential integrity: %d fact rows without matching dimension row', fact_orphans);
  END IF;

  COMMIT TRANSACTION;
EXCEPTION WHEN ERROR THEN
  ROLLBACK TRANSACTION;
  RAISE USING MESSAGE = FORMAT('10_load_core.sql failed, nothing changed: %s', @@error.message);
END;

SELECT
  ARRAY_LENGTH(run_days)                 AS days_loaded,
  run_days[OFFSET(0)]                    AS first_day,
  run_days[ORDINAL(ARRAY_LENGTH(run_days))] AS last_day,
  (SELECT COUNT(*) FROM core.stg_stop_event) AS fact_rows_written;
