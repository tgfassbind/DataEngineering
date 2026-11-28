-- Monitoring data-quality checks (docs/data_quality.md). Run after
-- 20_refresh_mart.sql for the date range of the pipeline run.
--
-- These checks never stop the pipeline: they record findings that need a
-- human look (a missing line, an unusual volume, many missing measurements,
-- old data) in core.dq_check_result and return them, so Kestra can log them
-- as warnings. Blocking checks are in 10_load_core.sql and 20_refresh_mart.sql.
--
-- Expects three variables, declared BEFORE this file (Kestra prepends them):
--   DECLARE p_range_start DATE DEFAULT DATE '2026-11-26';
--   DECLARE p_range_end   DATE DEFAULT DATE '2026-11-26';
--   DECLARE p_run_id      STRING DEFAULT 'manual';
-- To run it by hand, paste these three lines in front of the file.

DECLARE v_checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
DECLARE v_today DATE DEFAULT CURRENT_DATE('Europe/Zurich');

-- Thresholds
DECLARE v_history_days INT64 DEFAULT 28;      -- look-back for expected lines and volumes
DECLARE v_volume_tolerance FLOAT64 DEFAULT 0.5;   -- +/- 50 % of the usual row count
DECLARE v_min_measured_share FLOAT64 DEFAULT 0.8; -- at least 80 % of stops measured
DECLARE v_max_age_days INT64 DEFAULT 2;           -- newest operating day vs. today

CREATE TEMP TABLE findings (
  check_name STRING, betriebstag DATE, line_key INT64,
  observed FLOAT64, expected STRING, details STRING);

-- Rows per operating day and line: the range plus the look-back.
CREATE TEMP TABLE day_line AS
SELECT
  betriebstag,
  line_key,
  COUNT(*) AS n_rows,
  COUNTIF(sched_arr IS NOT NULL) AS n_arr_planned,
  COUNTIF(arr_delay_s IS NOT NULL) AS n_arr_measured,
  COUNTIF(sched_dep IS NOT NULL) AS n_dep_planned,
  COUNTIF(dep_delay_s IS NOT NULL) AS n_dep_measured
FROM core.fact_stop_event
WHERE betriebstag BETWEEN DATE_SUB(p_range_start, INTERVAL v_history_days DAY) AND p_range_end
GROUP BY betriebstag, line_key;

-- 1. line_missing_on_day: a line that had rows in the look-back has none on a
--    day of the range (strike, line not in the source, filter problem).
INSERT INTO findings
SELECT 'line_missing_on_day', d, h.line_key, 0, 'rows > 0',
       FORMAT('no rows, but %d day(s) with rows in the %d days before', COUNT(*), v_history_days)
FROM UNNEST(GENERATE_DATE_ARRAY(p_range_start, p_range_end)) AS d
JOIN day_line AS h
  ON h.betriebstag BETWEEN DATE_SUB(d, INTERVAL v_history_days DAY) AND DATE_SUB(d, INTERVAL 1 DAY)
WHERE NOT EXISTS (SELECT 1 FROM day_line AS c WHERE c.betriebstag = d AND c.line_key = h.line_key)
GROUP BY d, h.line_key;

-- 2. volume_anomaly: row count far from the median of the same weekday in the
--    look-back (needs at least 2 such days; holidays will show up here too).
INSERT INTO findings
SELECT 'volume_anomaly', betriebstag, line_key, n_rows,
       FORMAT('%d +/- %.0f %%', median_rows, v_volume_tolerance * 100),
       FORMAT('%d rows vs. median %d of the last %d same weekdays',
              n_rows, median_rows, history_days)
FROM (
  SELECT c.betriebstag, c.line_key, c.n_rows,
         APPROX_QUANTILES(h.n_rows, 2)[OFFSET(1)] AS median_rows,
         COUNT(*) AS history_days
  FROM day_line AS c
  JOIN day_line AS h
    ON h.line_key = c.line_key
   AND h.betriebstag BETWEEN DATE_SUB(c.betriebstag, INTERVAL v_history_days DAY)
                         AND DATE_SUB(c.betriebstag, INTERVAL 1 DAY)
   AND EXTRACT(DAYOFWEEK FROM h.betriebstag) = EXTRACT(DAYOFWEEK FROM c.betriebstag)
  WHERE c.betriebstag BETWEEN p_range_start AND p_range_end
  GROUP BY c.betriebstag, c.line_key, c.n_rows
)
WHERE history_days >= 2
  AND ABS(n_rows - median_rows) > v_volume_tolerance * median_rows;

-- 3. low_measurement_share: too few stops with a valid real-time measurement
--    (the medians of that day rest on few values).
INSERT INTO findings
SELECT 'low_measurement_share', betriebstag, line_key, share,
       FORMAT('>= %.0f %%', v_min_measured_share * 100),
       FORMAT('%s: %.1f %% of planned %s times measured', side, share * 100, side)
FROM (
  SELECT betriebstag, line_key, 'arrival' AS side,
         SAFE_DIVIDE(n_arr_measured, n_arr_planned) AS share
  FROM day_line WHERE betriebstag BETWEEN p_range_start AND p_range_end
  UNION ALL
  SELECT betriebstag, line_key, 'departure',
         SAFE_DIVIDE(n_dep_measured, n_dep_planned)
  FROM day_line WHERE betriebstag BETWEEN p_range_start AND p_range_end
)
WHERE share < v_min_measured_share;

-- 4. stale_data: the newest operating day in the fact table is too old
--    (the daily run did not happen or failed).
INSERT INTO findings
SELECT 'stale_data', newest, NULL, DATE_DIFF(v_today, newest, DAY),
       FORMAT('<= %d days', v_max_age_days),
       FORMAT('newest operating day %t is %d days old', newest, DATE_DIFF(v_today, newest, DAY))
FROM (SELECT MAX(betriebstag) AS newest FROM core.fact_stop_event)
WHERE newest IS NULL OR DATE_DIFF(v_today, newest, DAY) > v_max_age_days;

-- Record: one WARN row per finding, one PASS row per check without findings.
INSERT INTO core.dq_check_result (
  checked_at, run_id, range_start, range_end, check_name, status,
  betriebstag, line_key, observed, expected, details)
SELECT v_checked_at, p_run_id, p_range_start, p_range_end, check_name, 'WARN',
       betriebstag, line_key, observed, expected, details
FROM findings
UNION ALL
SELECT v_checked_at, p_run_id, p_range_start, p_range_end, c, 'PASS',
       NULL, NULL, NULL, NULL, NULL
FROM UNNEST(['line_missing_on_day', 'volume_anomaly', 'low_measurement_share', 'stale_data']) AS c
WHERE c NOT IN (SELECT check_name FROM findings);

-- Result for Kestra: the warnings of this run, with line names.
SELECT f.check_name, f.betriebstag, l.linien_text, l.betreiber_abk, f.observed, f.expected, f.details
FROM findings AS f
LEFT JOIN core.dim_line AS l ON l.line_key = f.line_key
ORDER BY f.check_name, f.betriebstag, l.linien_text;
