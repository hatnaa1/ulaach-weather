-- ============================================================
-- Зорилго: weather.gov.mn -> n8n -> Postgres цуглуулгын схем (v2, цэгцэлсэн)
-- Орчин:  PostgreSQL 13+
-- Ажиллуулах: psql -U postgres -d weather -f weather_schema.sql
--
-- v1-ээс ялгаа:
--   ХАССАН ХYCHYCGT
--     aws_data  -- зөвхөн УБ буцаадаг, obs_data-гийн бүрэн давхардал
--     sunrise   -- детерминистик тооцоо, доорх wx.solar_times() функцээр орлуулав
--   ХАССАН БАГАНА (obs_data)
--     temp_feels     -- мөрийн ~85%-д temp-тэй ижил, v_obs дотор бодогдоно
--     ptu_time       -- ихэвчлэн obs_time-тай ижил эсвэл хуучирсан
--     pressure_trend -- LAG(pressure_slp)-ээр гардаг
--     w1, w2         -- ихэвчлэн null, кодын хүснэгт нь тодорхойгүй
--
-- Тэмдэглэл:
--   * Бүх timestamptz нь UTC-д хадгалагдана; v_* view-үүд УБ цаг руу хөрвүүлнэ.
--   * row_key нь n8n-ий upsert түлхүүр. obs_data-д ТАТСАН цаг биш,
--     ХЭМЖСЭН цагаар (access_date) үүсдэг тул өдөрт 2-4 удаа мэдээлдэг
--     сумын станцууд шинэ мөр үүсгэхгүй, зөвхөн шинэчлэгдэнэ.
--   * Скрипт idempotent: дахин ажиллуулахад өгөгдөл устгахгүй.
-- ============================================================

CREATE SCHEMA IF NOT EXISTS wx;
SET search_path TO wx, public;

-- ------------------------------------------------------------
-- 1. Станцын лавлах (өдөрт 1 удаа шинэчлэгдэнэ, ~336 мөр тогтмол)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wx.stations (
  row_key      text PRIMARY KEY,
  sid          integer NOT NULL UNIQUE,
  api_id       integer,
  aimag_id     smallint,
  aimag_name   text,
  sum_name     text,
  sta_name     text,
  lat          double precision,
  lon          double precision,
  elev         integer,
  sta_type     smallint,
  sta_sort     smallint,
  sta_28       smallint,
  sta_tuimer   smallint,
  collected_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS stations_aimag_idx ON wx.stations (aimag_name);
CREATE INDEX IF NOT EXISTS stations_sta28_idx ON wx.stations (sta_28) WHERE sta_28 <> 0;

-- ------------------------------------------------------------
-- 2. Ажиглалт -- гол хүснэгт (~1.5 сая мөр/жил)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wx.obs_data (
  row_key      text PRIMARY KEY,
  sid          integer NOT NULL,
  obs_time     timestamptz NOT NULL,
  updated      timestamptz,
  collected_at timestamptz NOT NULL,
  temp         real,
  temp_min     real,
  temp_max     real,
  humidity     smallint,
  pressure_st  real,
  pressure_slp real,
  precip       real,
  wind_dir     smallint,
  wind_speed   real,
  wind_max     real,
  ww           smallint,
  cloud_nh     smallint,
  snow_depth   real,
  soil_tmin    real
);

CREATE INDEX IF NOT EXISTS obs_sid_time_idx  ON wx.obs_data (sid, obs_time DESC);
CREATE INDEX IF NOT EXISTS obs_time_brin_idx ON wx.obs_data USING brin (obs_time);

-- ------------------------------------------------------------
-- 3. Өдрийн урьдчилсан мэдээ -- ревиз бүрийг хадгална
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wx.fore_day (
  row_key        text PRIMARY KEY,
  sid            integer NOT NULL,
  fore_date      date NOT NULL,
  issued_at      timestamptz,
  collected_at   timestamptz NOT NULL,
  ww_code        smallint,
  precip_prob    smallint,
  temp_day       real,
  temp_day_feels real,
  wind_day       real
);

CREATE INDEX IF NOT EXISTS fore_day_sid_date_idx ON wx.fore_day (sid, fore_date DESC);
CREATE INDEX IF NOT EXISTS fore_day_issued_idx   ON wx.fore_day (issued_at DESC);

-- ------------------------------------------------------------
-- 4. 3 цаг тутмын урьдчилсан мэдээ (одоогоор зөвхөн УБ)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wx.fore_3hours (
  row_key      text PRIMARY KEY,
  sid          integer NOT NULL,
  f_time       timestamptz NOT NULL,
  issued_at    timestamptz NOT NULL,
  collected_at timestamptz NOT NULL,
  pre_code     smallint,
  temp         real,
  temp_feels   real,
  precip_mm    real,
  precip_prob  smallint,
  wind         real
);

CREATE INDEX IF NOT EXISTS fore3h_sid_time_idx ON wx.fore_3hours (sid, f_time DESC);

-- ------------------------------------------------------------
-- 5. Агаарын чанар
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wx.aqi_data (
  row_key      text PRIMARY KEY,
  sid          text NOT NULL,
  sname        text,
  lat          double precision,
  lon          double precision,
  obs_time     timestamptz NOT NULL,
  collected_at timestamptz NOT NULL,
  aqi          smallint,
  pm25_value   real, pm25_aqi smallint,
  pm10_value   real, pm10_aqi smallint,
  no2_value    real, no2_aqi  smallint,
  so2_value    real, so2_aqi  smallint,
  co_value     real, co_aqi   smallint,
  o3_value     real, o3_aqi   smallint
);

CREATE INDEX IF NOT EXISTS aqi_sid_time_idx  ON wx.aqi_data (sid, obs_time DESC);
CREATE INDEX IF NOT EXISTS aqi_time_brin_idx ON wx.aqi_data USING brin (obs_time);

-- ------------------------------------------------------------
-- 6. Мэдээ / анхааруулга
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wx.content (
  row_key      text PRIMARY KEY,
  api_id       integer NOT NULL,
  title        text,
  author       text,
  author_id    integer,
  status       text,
  created      timestamptz,
  valid_date   timestamptz,
  collected_at timestamptz NOT NULL,
  text_len     integer,
  body_text    text,
  images       text
);

CREATE INDEX IF NOT EXISTS content_created_idx ON wx.content (created DESC);
CREATE INDEX IF NOT EXISTS content_author_idx  ON wx.content (author);

-- ------------------------------------------------------------
-- 7. Ой хээрийн түймрийн эрсдэлийн индекс
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wx.tuimer (
  row_key      text PRIMARY KEY,
  aimag_name   text NOT NULL,
  sum_name     text NOT NULL,
  obs_date     date NOT NULL,
  lat          double precision,
  lon          double precision,
  idx          integer,
  collected_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS tuimer_date_idx ON wx.tuimer (obs_date DESC);
CREATE INDEX IF NOT EXISTS tuimer_idx_idx  ON wx.tuimer (idx DESC);

-- ============================================================
-- ФУНКЦ: нар мандах / жаргах -- obs/sunrise endpoint-ийг орлоно
-- ============================================================
CREATE OR REPLACE FUNCTION wx.solar_times(
  p_lat  double precision,
  p_lon  double precision,
  p_date date,
  p_tz_offset_min integer DEFAULT 480
)
RETURNS TABLE (sunrise time, sunset time, day_length_min integer)
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  doy    integer;
  g      double precision;
  eqtime double precision;
  decl   double precision;
  latr   double precision;
  x      double precision;
  ha     double precision;
  rise_m double precision;
  set_m  double precision;
  len_m  double precision;
BEGIN
  doy  := extract(doy FROM p_date);
  latr := radians(p_lat);

  g := 2 * pi() / 365 * (doy - 1 + 0.5);

  eqtime := 229.18 * (0.000075
                      + 0.001868 * cos(g)     - 0.032077 * sin(g)
                      - 0.014615 * cos(2 * g) - 0.040849 * sin(2 * g));

  decl := 0.006918
          - 0.399912 * cos(g)     + 0.070257 * sin(g)
          - 0.006758 * cos(2 * g) + 0.000907 * sin(2 * g)
          - 0.002697 * cos(3 * g) + 0.00148  * sin(3 * g);

  x := cos(radians(90.833)) / (cos(latr) * cos(decl)) - tan(latr) * tan(decl);

  IF x < -1 OR x > 1 THEN
    RETURN QUERY SELECT NULL::time, NULL::time, NULL::integer;
    RETURN;
  END IF;

  ha := degrees(acos(x));

  rise_m := 720 - 4 * (p_lon + ha) - eqtime + p_tz_offset_min;
  set_m  := 720 - 4 * (p_lon - ha) - eqtime + p_tz_offset_min;
  len_m  := set_m - rise_m;

  rise_m := rise_m - floor(rise_m / 1440) * 1440;
  set_m  := set_m  - floor(set_m  / 1440) * 1440;

  RETURN QUERY SELECT
    ('00:00:00'::time + (rise_m * interval '1 minute'))::time,
    ('00:00:00'::time + (set_m  * interval '1 minute'))::time,
    round(len_m)::integer;
END;
$$;

CREATE OR REPLACE VIEW wx.v_sunrise_today AS
SELECT
  s.sid, s.aimag_name, s.sum_name, s.lat, s.lon,
  current_date AS obs_date,
  t.sunrise, t.sunset, t.day_length_min
FROM wx.stations s
CROSS JOIN LATERAL wx.solar_times(s.lat, s.lon, current_date) t
WHERE s.lat IS NOT NULL AND s.lon IS NOT NULL;

-- ============================================================
-- VIEW-YYD
-- ============================================================

CREATE OR REPLACE VIEW wx.v_obs AS
SELECT
  o.sid,
  s.aimag_name, s.sum_name, s.sta_name,
  s.lat, s.lon, s.elev,
  (o.obs_time AT TIME ZONE 'Asia/Ulaanbaatar')       AS obs_time_ub,
  (o.obs_time AT TIME ZONE 'Asia/Ulaanbaatar')::date AS obs_date_ub,
  o.temp,
  CASE
    WHEN o.temp <= 10 AND o.wind_speed > 1.34 THEN
      round((13.12 + 0.6215 * o.temp
             - 11.37 * power(o.wind_speed * 3.6, 0.16)
             + 0.3965 * o.temp * power(o.wind_speed * 3.6, 0.16))::numeric, 1)
    ELSE round(o.temp::numeric, 1)
  END AS temp_feels,
  o.humidity, o.precip,
  o.wind_speed, o.wind_max, o.wind_dir,
  o.pressure_st, o.pressure_slp,
  o.cloud_nh, o.snow_depth, o.soil_tmin,
  o.ww,
  o.obs_time
FROM wx.obs_data o
LEFT JOIN wx.stations s USING (sid);

CREATE OR REPLACE VIEW wx.v_obs_latest AS
SELECT DISTINCT ON (o.sid)
  o.sid,
  s.aimag_name, s.sum_name, s.lat, s.lon, s.elev, s.sta_type,
  (o.obs_time AT TIME ZONE 'Asia/Ulaanbaatar') AS obs_time_ub,
  round((extract(epoch FROM (now() - o.obs_time)) / 3600.0)::numeric, 1) AS age_hours,
  o.temp, o.humidity, o.precip,
  o.wind_speed, o.wind_dir, o.pressure_slp, o.cloud_nh
FROM wx.obs_data o
LEFT JOIN wx.stations s USING (sid)
ORDER BY o.sid, o.obs_time DESC;

CREATE OR REPLACE VIEW wx.v_obs_daily AS
SELECT
  o.sid,
  (o.obs_time AT TIME ZONE 'Asia/Ulaanbaatar')::date AS obs_date,
  min(o.temp)                        AS temp_min,
  max(o.temp)                        AS temp_max,
  round(avg(o.temp)::numeric, 1)     AS temp_avg,
  max(o.precip)                      AS precip_max,
  max(o.wind_max)                    AS wind_max,
  round(avg(o.humidity)::numeric, 0) AS humidity_avg,
  count(*)                           AS obs_count
FROM wx.obs_data o
GROUP BY 1, 2;

CREATE OR REPLACE VIEW wx.v_forecast_accuracy AS
SELECT
  f.sid,
  s.aimag_name,
  s.sum_name,
  f.fore_date,
  (f.issued_at AT TIME ZONE 'Asia/Ulaanbaatar') AS issued_at_ub,
  (f.fore_date - (f.issued_at AT TIME ZONE 'Asia/Ulaanbaatar')::date) AS lead_days,
  f.temp_day        AS forecast_temp,
  d.temp_max        AS actual_temp_max,
  round((f.temp_day - d.temp_max)::numeric, 1)      AS error_c,
  abs(round((f.temp_day - d.temp_max)::numeric, 1)) AS abs_error_c,
  f.precip_prob,
  d.precip_max      AS actual_precip
FROM wx.fore_day f
JOIN wx.v_obs_daily d ON d.sid = f.sid AND d.obs_date = f.fore_date
LEFT JOIN wx.stations s USING (sid);

CREATE OR REPLACE VIEW wx.v_aqi_latest AS
SELECT DISTINCT ON (sid)
  sid, sname, lat, lon,
  (obs_time AT TIME ZONE 'Asia/Ulaanbaatar') AS obs_time_ub,
  aqi, pm25_value, pm10_value, so2_value, no2_value, o3_value, co_value
FROM wx.aqi_data
ORDER BY sid, obs_time DESC;
