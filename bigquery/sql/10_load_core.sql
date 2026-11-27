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
-- Idempotent: running it twice with the same load table gives the same result.
-- SCD type 1 with a guard: attributes are only overwritten by data from the
-- same or a later operating day, so a backfill of old days never replaces a
-- newer stop name with an older one.

DECLARE run_days ARRAY<DATE> DEFAULT (
  SELECT ARRAY_AGG(DISTINCT betriebstag ORDER BY betriebstag)
  FROM core.stg_run_scope
);

IF run_days IS NULL THEN
  SELECT 'core.stg_run_scope is empty, nothing loaded' AS info;
  RETURN;
END IF;

-- Every row must belong to a processed day and line, otherwise the delete
-- below would not cover it and the insert would duplicate it.
ASSERT NOT EXISTS (
  SELECT 1
  FROM core.stg_stop_event AS e
  LEFT JOIN core.stg_run_scope AS s
    USING (betriebstag, linien_text, betreiber_abk)
  WHERE s.betriebstag IS NULL
) AS 'core.stg_stop_event has rows outside core.stg_run_scope, nothing loaded';

-- BigQuery does not enforce primary keys: check the fact key before loading.
ASSERT NOT EXISTS (
  SELECT 1
  FROM core.stg_stop_event
  GROUP BY betriebstag, fahrt_bezeichner, bpuic
  HAVING COUNT(*) > 1
) AS 'core.stg_stop_event: duplicate (betriebstag, fahrt_bezeichner, bpuic), nothing loaded';

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
