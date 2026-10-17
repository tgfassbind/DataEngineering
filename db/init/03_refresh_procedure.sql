-- Runs once, when the PostgreSQL container starts with an EMPTY data volume
-- (docker-entrypoint-initdb.d, after 02_reporting_tables.sql).
--
-- Aggregation step staging -> reporting. Call it once after the last
-- transformed day of a run:
--   docker compose exec database sh -c \
--     'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CALL reporting.refresh_od_delay();"'

-- Remove an older signature first: CREATE OR REPLACE with a different
-- argument list would add an overload and make "CALL ...()" ambiguous.
DROP PROCEDURE IF EXISTS reporting.refresh_od_delay();

CREATE OR REPLACE PROCEDURE reporting.refresh_od_delay(lock_wait interval DEFAULT interval '30 seconds')
LANGUAGE plpgsql
-- SET clauses are restored when the procedure returns, so the caller's
-- session settings are untouched (even inside an explicit transaction).
SET search_path = pg_catalog, pg_temp
SET lock_timeout = '30s'
AS $proc$
DECLARE
    -- Fixed key for the advisory lock (advisory locks are per database);
    -- 8502202 = BPUIC of Rotkreuz, chosen only to be recognisable in pg_locks.
    c_lock_key  CONSTANT bigint := 8502202;
    t_start     timestamptz := clock_timestamp();
    t_locked    timestamptz;
    n_deleted   bigint;
    n_written   bigint;
    n_pairs     bigint;
BEGIN
    -- upper bound: lock_timeout is an int in milliseconds (max ~24.8 days)
    IF lock_wait IS NULL OR lock_wait < interval '0' OR lock_wait > interval '24 days' THEN
        RAISE EXCEPTION 'reporting.refresh_od_delay: lock_wait must be between 0 and 24 days (0 = wait without limit), got %', lock_wait
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- lock_timeout in milliseconds, local to this transaction and, through the
    -- SET clause above, reset when the procedure returns. It stays in force for
    -- the DELETE/INSERT too: a conflicting lock on reporting/staging (e.g. a
    -- long ALTER TABLE) also makes the CALL fail after lock_wait, never hang.
    PERFORM set_config('lock_timeout', ceil(extract(epoch FROM lock_wait) * 1000)::bigint::text, true);

    -- One refresh at a time. A second CALL waits for the first one's
    -- transaction to end (it then recomputes from the committed staging data)
    -- or fails with SQLSTATE 55P03 after lock_wait.
    BEGIN
        PERFORM pg_advisory_xact_lock(c_lock_key);
    EXCEPTION WHEN lock_not_available THEN
        RAISE EXCEPTION 'reporting.refresh_od_delay: another refresh is still running (waited %), nothing changed', lock_wait
            USING ERRCODE = 'lock_not_available',
                  HINT = 'Wait for the running CALL to finish, then call again.';
    END;
    t_locked := clock_timestamp();

    -- Full recompute in the CALL's own transaction. DELETE (not TRUNCATE):
    -- readers keep seeing the old content until the commit, and any error
    -- below rolls everything back, so the old table stays intact.
    DELETE FROM reporting.od_delay_by_slot;
    GET DIAGNOSTICS n_deleted = ROW_COUNT;

    INSERT INTO reporting.od_delay_by_slot (
        linien_text, betreiber_abk,
        from_bpuic, from_stop, to_bpuic, to_stop,
        weekday, slot_hour,
        median_dep_delay_min, median_arr_delay_min,
        n_dep_obs, n_arr_obs)
    SELECT
        a.linien_text,
        a.betreiber_abk,
        a.bpuic,
        min(a.haltestellen_name COLLATE "C"),     -- byte order: same result on every server locale
        b.bpuic,
        min(b.haltestellen_name COLLATE "C"),
        extract(isodow FROM a.sched_dep)::smallint,
        extract(hour   FROM a.sched_dep)::smallint,
        -- percentile_cont over integer seconds is exact in double precision
        -- (a value or the mean of two); numeric division + one round(numeric):
        -- half away from zero, and numeric has no -0.0.
        round((percentile_cont(0.5) WITHIN GROUP (ORDER BY a.dep_delay_s))::numeric / 60.0, 1),
        round((percentile_cont(0.5) WITHIN GROUP (ORDER BY b.arr_delay_s))::numeric / 60.0, 1),
        count(a.dep_delay_s),
        count(b.arr_delay_s)
    FROM staging.stop_events AS a
    JOIN staging.stop_events AS b
      ON  b.betriebstag      = a.betriebstag
      AND b.fahrt_bezeichner = a.fahrt_bezeichner
      AND b.stop_sequence    > a.stop_sequence
    WHERE a.sched_dep IS NOT NULL
    GROUP BY a.linien_text, a.betreiber_abk, a.bpuic, b.bpuic,
             extract(isodow FROM a.sched_dep), extract(hour FROM a.sched_dep)
    HAVING count(a.dep_delay_s) > 0 OR count(b.arr_delay_s) > 0;
    GET DIAGNOSTICS n_written = ROW_COUNT;

    SELECT count(*) INTO n_pairs
    FROM (SELECT DISTINCT from_bpuic, to_bpuic FROM reporting.od_delay_by_slot) AS p;

    RAISE NOTICE 'reporting.refresh_od_delay: rows_deleted=% rows_written=% distinct_pairs=% duration=%s (lock_waited=%s)',
        n_deleted, n_written, n_pairs,
        round(extract(epoch FROM clock_timestamp() - t_start), 3),
        round(extract(epoch FROM t_locked - t_start), 3);
END;
$proc$;

COMMENT ON PROCEDURE reporting.refresh_od_delay(interval) IS
    'Recomputes reporting.od_delay_by_slot in full from staging.stop_events (DELETE + INSERT in the caller''s '
    'transaction, no COMMIT inside; idempotent). Concurrent calls are serialised by an advisory lock; a call that '
    'cannot get it within lock_wait (default 30 s, 0 = no limit) fails with SQLSTATE 55P03 and changes nothing.';