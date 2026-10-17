-- Acceptance check for the fixture day (dataset/excerpt_21092026_istdaten_v2.csv,
-- 2026-09-21, IR75 + S1 via Rotkreuz). Run AFTER: seed -> transform -> CALL.
-- Fails with an error (exit code 3 with ON_ERROR_STOP) on the first mismatch,
-- prints "acceptance OK" otherwise. Only reads; changes nothing.
--
--   docker compose exec -T database sh -c \
--     'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < db/tests/acceptance_fixture.sql
--
-- Only valid when staging holds exactly the fixture day (single day: every cell has n = 1),
-- transformed WITHOUT the via filter (the default lines.yml keeps only trips via Rotkreuz):
--   docker compose run --rm transform --date 2026-09-21 --lines IR75:SBB,S1:SBB

DO $acceptance$
DECLARE
    r record;
BEGIN
    -- staging
    SELECT count(*) AS n, count(DISTINCT (betriebstag, fahrt_bezeichner)) AS trips INTO r FROM staging.stop_events;
    IF NOT (r.n = 445 AND r.trips = 51) THEN RAISE EXCEPTION '%', format('staging: %s rows / %s trips, expected 445 / 51', r.n, r.trips); END IF;

    -- reporting totals
    SELECT count(*) AS n, count(DISTINCT (from_bpuic, to_bpuic)) AS pairs,
           sum(n_dep_obs) AS sum_dep, count(*) FILTER (WHERE n_dep_obs <> 1) AS dep_not_1,
           count(*) FILTER (WHERE n_arr_obs = 1) AS arr_1, count(*) FILTER (WHERE n_arr_obs = 0) AS arr_0,
           count(*) FILTER (WHERE n_dep_obs = 0 AND n_arr_obs = 0) AS both_0,
           count(*) FILTER (WHERE weekday = 2) AS wd2,
           count(*) FILTER (WHERE weekday NOT IN (1, 2)) AS wd_other,
           array_agg(DISTINCT slot_hour ORDER BY slot_hour) AS hours
      INTO r FROM reporting.od_delay_by_slot;
    IF NOT (r.n = 1914) THEN RAISE EXCEPTION '%', format('reporting rows: %s, expected 1914', r.n); END IF;
    IF NOT (r.pairs = 132) THEN RAISE EXCEPTION '%', format('distinct (from_bpuic, to_bpuic): %s, expected 132', r.pairs); END IF;
    IF NOT (r.sum_dep = 1914 AND r.dep_not_1 = 0) THEN RAISE EXCEPTION '%', format('n_dep_obs: sum %s, rows <> 1: %s', r.sum_dep, r.dep_not_1); END IF;
    IF NOT (r.arr_1 = 1757 AND r.arr_0 = 157) THEN RAISE EXCEPTION '%', format('n_arr_obs: =1 on %s, =0 on %s, expected 1757 / 157', r.arr_1, r.arr_0); END IF;
    IF NOT (r.both_0 = 0) THEN RAISE EXCEPTION '%', 'rows with both counts 0'; END IF;
    IF NOT (r.wd2 = 6 AND r.wd_other = 0) THEN RAISE EXCEPTION '%', format('weekday 2 rows: %s (expected 6), other weekdays: %s', r.wd2, r.wd_other); END IF;
    IF NOT (r.hours = ARRAY[0,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23]::smallint[]) THEN RAISE EXCEPTION '%', format('slot hours: %s', r.hours); END IF;

    -- spot checks (dep / arr medians in minutes)
    FOR r IN SELECT * FROM (VALUES
            (8503000, 8505000, 1,  5, 0.8,  0.0),
            (8503000, 8505000, 1,  7, 1.6,  1.7),
            (8503000, 8505000, 1, 10, 6.3,  3.5),
            (8503000, 8502202, 1,  5, 0.8, -0.4),
            (8503000, 8502202, 1,  7, 1.6,  1.5),
            (8503000, 8502202, 1, 23, 0.8,  1.0),
            (8502204, 8505000, 2,  0, 2.3,  2.6)) AS v(f, t, wd, h, dep, arr)
    LOOP
        PERFORM 1 FROM reporting.od_delay_by_slot o
         WHERE o.linien_text = 'IR75' AND o.betreiber_abk = 'SBB' AND o.from_bpuic = r.f AND o.to_bpuic = r.t AND o.weekday = r.wd AND o.slot_hour = r.h
           AND o.median_dep_delay_min = r.dep AND o.median_arr_delay_min = r.arr
           AND o.n_dep_obs = 1 AND o.n_arr_obs = 1;
        IF NOT (FOUND) THEN RAISE EXCEPTION '%', format('spot check %s -> %s weekday %s hour %s: expected %s / %s', r.f, r.t, r.wd, r.h, r.dep, r.arr); END IF;
    END LOOP;

    -- the weekday-2 / hour-0 cell comes from the trip across midnight
    PERFORM 1 FROM staging.stop_events
     WHERE fahrt_bezeichner = 'ch:1:sjyid:100001:2592-001' AND bpuic = 8502204
       AND extract(isodow FROM sched_dep) = 2 AND extract(hour FROM sched_dep) = 0;
    IF NOT (FOUND) THEN RAISE EXCEPTION '%', 'trip ch:1:sjyid:100001:2592-001 does not depart Zug on Tuesday hour 0'; END IF;

    -- row counts per pair
    FOR r IN SELECT * FROM (VALUES
            (8503000, 8502202, 1, 19),   -- Zürich HB -> Rotkreuz, weekday 1, hours 5..23
            (8505000, 8503000, NULL, 19),  -- Luzern -> Zürich HB
            (8502202, 8505000, NULL, 19))  -- Rotkreuz -> Luzern
            AS v(f, t, wd, expected)
    LOOP
        PERFORM 1 FROM (SELECT count(*) AS n, min(slot_hour) AS lo, max(slot_hour) AS hi
                          FROM reporting.od_delay_by_slot
                         WHERE from_bpuic = r.f AND to_bpuic = r.t AND (r.wd IS NULL OR weekday = r.wd)) c
         WHERE c.n = r.expected AND (r.wd IS NULL OR (c.lo = 5 AND c.hi = 23));
        IF NOT (FOUND) THEN RAISE EXCEPTION '%', format('%s -> %s: expected %s rows', r.f, r.t, r.expected); END IF;
    END LOOP;

    RAISE NOTICE 'acceptance OK';
END
$acceptance$;