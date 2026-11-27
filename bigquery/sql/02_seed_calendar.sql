-- Generated dimensions: calendar days and hour slots.
-- Run once at setup, after 01_tables.sql. Idempotent: only missing rows are
-- inserted, so the range can be extended later by changing the dates below.

MERGE core.dim_date AS t
USING (
  SELECT
    d                                            AS date,
    MOD(EXTRACT(DAYOFWEEK FROM d) + 5, 7) + 1    AS iso_weekday,   -- DAYOFWEEK: 1 = Sunday
    FORMAT_DATE('%A', d)                         AS weekday_name,
    EXTRACT(DAYOFWEEK FROM d) IN (1, 7)          AS is_weekend,
    EXTRACT(ISOWEEK FROM d)                      AS iso_week,
    EXTRACT(MONTH FROM d)                        AS month,
    EXTRACT(YEAR FROM d)                         AS year
  FROM UNNEST(GENERATE_DATE_ARRAY(DATE '2018-01-01', DATE '2035-12-31')) AS d
) AS s
ON t.date = s.date
WHEN NOT MATCHED THEN
  INSERT (date, iso_weekday, weekday_name, is_weekend, iso_week, month, year)
  VALUES (s.date, s.iso_weekday, s.weekday_name, s.is_weekend, s.iso_week, s.month, s.year);

MERGE core.dim_time_slot AS t
USING (
  SELECT h AS slot_hour, FORMAT('%02d:00-%02d:59', h, h) AS label
  FROM UNNEST(GENERATE_ARRAY(0, 23)) AS h
) AS s
ON t.slot_hour = s.slot_hour
WHEN NOT MATCHED THEN
  INSERT (slot_hour, label) VALUES (s.slot_hour, s.label);
