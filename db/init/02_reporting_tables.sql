-- Runs once, when the PostgreSQL container starts with an EMPTY data volume
-- (docker-entrypoint-initdb.d, after 01_schemas.sql). Filled only by
-- CALL reporting.refresh_od_delay() (03_refresh_procedure.sql).
-- Schema changes need a fresh DB volume:
--   docker compose down && docker volume rm deng-datapipeline_db-data
-- (never "down -v": that also deletes the raw files in object storage).

CREATE TABLE IF NOT EXISTS reporting.od_delay_by_slot (
    linien_text           text          NOT NULL,
    betreiber_abk         text          NOT NULL,
    from_bpuic            integer       NOT NULL,
    from_stop             text,
    to_bpuic              integer       NOT NULL,
    to_stop               text,
    weekday               smallint      NOT NULL,
    slot_hour             smallint      NOT NULL,
    median_dep_delay_min  numeric(6,1)  NULL,
    median_arr_delay_min  numeric(6,1)  NULL,
    n_dep_obs             integer       NOT NULL,
    n_arr_obs             integer       NOT NULL,
    CONSTRAINT od_delay_by_slot_pk
        PRIMARY KEY (linien_text, betreiber_abk, from_bpuic, to_bpuic, weekday, slot_hour),
    CONSTRAINT od_delay_by_slot_weekday_iso   CHECK (weekday BETWEEN 1 AND 7),
    CONSTRAINT od_delay_by_slot_slot_hour     CHECK (slot_hour BETWEEN 0 AND 23),
    CONSTRAINT od_delay_by_slot_counts_nonneg CHECK (n_dep_obs >= 0 AND n_arr_obs >= 0),
    CONSTRAINT od_delay_by_slot_has_obs       CHECK (n_dep_obs > 0 OR n_arr_obs > 0),
    CONSTRAINT od_delay_by_slot_dep_null_iff_no_obs
        CHECK ((median_dep_delay_min IS NULL) = (n_dep_obs = 0)),
    CONSTRAINT od_delay_by_slot_arr_null_iff_no_obs
        CHECK ((median_arr_delay_min IS NULL) = (n_arr_obs = 0))
);

-- The app looks up by line + stop names + weekday + slot.
CREATE INDEX IF NOT EXISTS od_delay_by_slot_lookup_idx
    ON reporting.od_delay_by_slot (linien_text, betreiber_abk, from_stop, to_stop, weekday, slot_hour);

COMMENT ON TABLE reporting.od_delay_by_slot IS
    'Data product: median delay per line, origin-destination stop pair (every ordered pair of stops of a trip, '
    'not only neighbours), ISO weekday and one-hour slot of the planned departure at the origin. '
    'Recomputed in full from staging.stop_events by CALL reporting.refresh_od_delay().';
COMMENT ON COLUMN reporting.od_delay_by_slot.linien_text IS 'Line (LINIEN_TEXT), e.g. IR75. Only unique together with betreiber_abk.';
COMMENT ON COLUMN reporting.od_delay_by_slot.betreiber_abk IS 'Operator abbreviation (BETREIBER_ABK), e.g. SBB.';
COMMENT ON COLUMN reporting.od_delay_by_slot.from_bpuic IS 'Origin stop (BPUIC), where the passenger boards.';
COMMENT ON COLUMN reporting.od_delay_by_slot.from_stop IS 'Origin stop name: min() over the group (byte order, collation-independent).';
COMMENT ON COLUMN reporting.od_delay_by_slot.to_bpuic IS 'Destination stop (BPUIC), any later stop of the same trip.';
COMMENT ON COLUMN reporting.od_delay_by_slot.to_stop IS 'Destination stop name: min() over the group (byte order, collation-independent).';
COMMENT ON COLUMN reporting.od_delay_by_slot.weekday IS 'ISO weekday (1 = Monday .. 7 = Sunday) of the planned departure at the origin.';
COMMENT ON COLUMN reporting.od_delay_by_slot.slot_hour IS 'Hour (0..23) of the planned departure at the origin, naive Swiss local time.';
COMMENT ON COLUMN reporting.od_delay_by_slot.median_dep_delay_min IS
    'Median departure delay at the origin in minutes (signed, negative = early), rounded once to 0.1 '
    '(half away from zero). NULL exactly when n_dep_obs = 0.';
COMMENT ON COLUMN reporting.od_delay_by_slot.median_arr_delay_min IS
    'Median arrival delay at the destination in minutes (signed, negative = early), rounded once to 0.1 '
    '(half away from zero). NULL exactly when n_arr_obs = 0.';
COMMENT ON COLUMN reporting.od_delay_by_slot.n_dep_obs IS
    'Number of non-NULL departure delays behind median_dep_delay_min (one per trip and operating day). No minimum threshold.';
COMMENT ON COLUMN reporting.od_delay_by_slot.n_arr_obs IS
    'Number of non-NULL arrival delays behind median_arr_delay_min (one per trip and operating day). No minimum threshold.';