-- Runs once, when the PostgreSQL container starts with an EMPTY data volume
-- (docker-entrypoint-initdb.d). To re-run it: docker compose down -v (deletes
-- ALL database data) or apply it manually with psql. Applying it twice is
-- harmless, but IF NOT EXISTS also means an existing table with an older
-- definition is NOT changed: schema changes need a migration or a fresh volume.

CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS reporting;   -- filled later by the aggregation step

COMMENT ON SCHEMA staging IS
    'Cleaned per-observation data written by the transform job (one row per trip x stop).';
COMMENT ON SCHEMA reporting IS
    'Aggregated data product (median delays). Populated by a later pipeline step.';

CREATE TABLE IF NOT EXISTS staging.stop_events (
    betriebstag         date        NOT NULL,
    fahrt_bezeichner    text        NOT NULL,
    bpuic               integer     NOT NULL,
    haltestellen_name   text,
    linien_text         text        NOT NULL,
    betreiber_abk       text        NOT NULL,
    stop_sequence       integer     NOT NULL,
    sched_arr           timestamp,              -- naive Swiss local time
    sched_dep           timestamp,              -- naive Swiss local time
    arr_delay_s         integer     NULL,
    dep_delay_s         integer     NULL,
    is_additional_trip  boolean     NOT NULL,
    row_hash            text        NOT NULL,
    loaded_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT stop_events_pk PRIMARY KEY (betriebstag, fahrt_bezeichner, bpuic),
    CONSTRAINT stop_events_seq_positive CHECK (stop_sequence >= 1),
    CONSTRAINT stop_events_hash_sha256 CHECK (row_hash ~ '^[0-9a-f]{64}$')
);

-- The aggregation reads one day and one line at a time.
CREATE INDEX IF NOT EXISTS stop_events_day_line_idx
    ON staging.stop_events (betriebstag, linien_text, betreiber_abk);

COMMENT ON TABLE staging.stop_events IS
    'One row per served stop of a trip on one operating day (cancelled stops and pass-throughs are not stored). '
    'Delays are per observation, signed and unclipped; NULL means "no valid measurement", never 0.';
COMMENT ON COLUMN staging.stop_events.betriebstag IS 'Operating day (BETRIEBSTAG); equals the date of the raw file.';
COMMENT ON COLUMN staging.stop_events.fahrt_bezeichner IS 'Trip identifier (FAHRT_BEZEICHNER), unique within an operating day.';
COMMENT ON COLUMN staging.stop_events.bpuic IS 'Stop identifier (BPUIC).';
COMMENT ON COLUMN staging.stop_events.haltestellen_name IS 'Stop name (HALTESTELLEN_NAME) as delivered.';
COMMENT ON COLUMN staging.stop_events.linien_text IS 'Line (LINIEN_TEXT), e.g. IR75. Only unique together with betreiber_abk.';
COMMENT ON COLUMN staging.stop_events.betreiber_abk IS 'Operator abbreviation (BETREIBER_ABK), e.g. SBB.';
COMMENT ON COLUMN staging.stop_events.stop_sequence IS
    '1-based position of the stop within the trip, ordered by planned time (arrival, else departure). '
    'Counts served stops only (no cancelled stops, pass-throughs or duplicates), so it is gap-free.';
COMMENT ON COLUMN staging.stop_events.sched_arr IS 'Planned arrival (ANKUNFTSZEIT), naive local time; NULL at the first stop.';
COMMENT ON COLUMN staging.stop_events.sched_dep IS 'Planned departure (ABFAHRTSZEIT), naive local time; NULL at the last stop.';
COMMENT ON COLUMN staging.stop_events.arr_delay_s IS
    'AN_PROGNOSE - ANKUNFTSZEIT in seconds; negative = early. NULL if undefined or the measurement failed validation.';
COMMENT ON COLUMN staging.stop_events.dep_delay_s IS
    'AB_PROGNOSE - ABFAHRTSZEIT in seconds; negative = early. NULL if undefined or the measurement failed validation.';
COMMENT ON COLUMN staging.stop_events.is_additional_trip IS 'ZUSATZFAHRT_TF: trip not in the regular timetable.';
COMMENT ON COLUMN staging.stop_events.row_hash IS
    'SHA-256 (hex) of the canonical string of all stored value columns except row_hash and loaded_at; drives the upsert.';
COMMENT ON COLUMN staging.stop_events.loaded_at IS 'Time the row was last inserted or changed (unchanged rows keep their value).';
