-- =============================================================================
-- build_locatron_street.sql
-- Derived AU street gazetteer for Locatron.
--
-- Supersedes the single-statement INSERT, which failed with:
--   Error 1062: Duplicate entry 'NSW-RYDE-HAMILTON CR-2112' for key PRIMARY
--
-- Two problems in that version:
--
--   1. street_key is derived from (STREET_NAME, STREET_TYPE, STREET_SUFFIX), but
--      the primary key is (state, locality, street_key, postcode). Several
--      distinct decompositions collapse to one key -- ('HAMILTON','CR','') and
--      ('HAMILTON CR','','') both produce 'HAMILTON CR'. Grouping on all seven
--      columns therefore emits multiple rows per primary key.
--
--   2. CAST('' AS DECIMAL) returns 0, not NULL. Averaging that pulls street
--      centroids toward (0,0) proportionally to how many blank coordinates the
--      street has. Every cast needs NULLIF(TRIM(col),'') inside it.
--
-- Fix: aggregate to variant level first, then collapse to primary-key level,
-- choosing one canonical decomposition per key and summing counts across all
-- variants so no addresses are lost.
--
-- RUNTIME: Step 2 is a full scan + aggregate over ~15M rows. ~8 min.
-- =============================================================================

USE ReferenceDB;

SET SESSION sql_mode = 'STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION';
SET SESSION group_concat_max_len = 1048576;


-- -----------------------------------------------------------------------------
-- STEP 1. Diagnostics. Worth running before the rebuild so you know the scale
--         of the collision problem in your data.
-- -----------------------------------------------------------------------------

-- 1a. The specific reported collision.
SELECT STREET_NAME, STREET_TYPE, STREET_SUFFIX, COUNT(*) AS n
FROM address_ref
WHERE STATE = 'NSW' AND LOCALITY_NAME = 'RYDE' AND POSTCODE = '2112'
  AND UPPER(CONCAT_WS(' ', STREET_NAME, NULLIF(STREET_TYPE,''), NULLIF(STREET_SUFFIX,'')))
      = 'HAMILTON CR'
GROUP BY 1, 2, 3;

-- 1b. All colliding keys, worst first. Expect a few thousand across 15M rows.
SELECT STATE, LOCALITY_NAME, POSTCODE,
       UPPER(CONCAT_WS(' ', STREET_NAME, NULLIF(STREET_TYPE,''), NULLIF(STREET_SUFFIX,'')))
         AS street_key,
       COUNT(DISTINCT CONCAT_WS('\t', STREET_NAME,
                                COALESCE(STREET_TYPE,''),
                                COALESCE(STREET_SUFFIX,''))) AS variants,
       GROUP_CONCAT(DISTINCT CONCAT('[', STREET_NAME, '|',
                                    COALESCE(STREET_TYPE,''), '|',
                                    COALESCE(STREET_SUFFIX,''), ']')
                    ORDER BY STREET_NAME SEPARATOR ' ') AS forms
FROM address_ref
WHERE NULLIF(TRIM(STREET_NAME), '') IS NOT NULL
GROUP BY 1, 2, 3, 4
HAVING variants > 1
ORDER BY variants DESC
LIMIT 50;

-- 1c. How many rows have unusable coordinates. These are the ones that were
--     being silently cast to 0.
SELECT COUNT(*) AS blank_or_null_coords
FROM address_ref
WHERE NULLIF(TRIM(LATITUDE), '') IS NULL
   OR NULLIF(TRIM(LONGITUDE), '') IS NULL;


-- -----------------------------------------------------------------------------
-- STEP 2. Variant-level aggregate.
--
--   Note the TRIM and UPPER applied to street_name / street_type / street_suffix
--   individually. That alone eliminates the collisions caused by case and
--   whitespace differences; only genuine decomposition differences survive to
--   Step 3.
-- -----------------------------------------------------------------------------

DROP TABLE IF EXISTS _ls_variant;
CREATE TABLE _ls_variant (
  state         CHAR(3)        NOT NULL,
  locality      VARCHAR(64)    NOT NULL,
  postcode      CHAR(4)        NOT NULL,
  street_key    VARCHAR(128)   NOT NULL,
  street_name   VARCHAR(64)    NOT NULL,
  street_type   VARCHAR(32)    NOT NULL DEFAULT '',
  street_suffix VARCHAR(16)    NOT NULL DEFAULT '',
  n             INT UNSIGNED   NOT NULL,
  lat_sum       DECIMAL(24,8)  NULL,
  lng_sum       DECIMAL(24,8)  NULL,
  geo_n         INT UNSIGNED   NOT NULL,
  KEY ix_lsv_pk (state, locality, postcode, street_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO _ls_variant
  (state, locality, postcode, street_key,
   street_name, street_type, street_suffix, n, lat_sum, lng_sum, geo_n)
SELECT
  UPPER(TRIM(STATE)),
  UPPER(TRIM(LOCALITY_NAME)),
  LPAD(TRIM(POSTCODE), 4, '0'),
  UPPER(TRIM(CONCAT_WS(' ',
       TRIM(STREET_NAME),
       NULLIF(TRIM(STREET_TYPE),   ''),
       NULLIF(TRIM(STREET_SUFFIX), '')))),
  UPPER(TRIM(STREET_NAME)),
  UPPER(COALESCE(TRIM(STREET_TYPE),   '')),
  UPPER(COALESCE(TRIM(STREET_SUFFIX), '')),
  COUNT(*),
  SUM(CAST(NULLIF(TRIM(LATITUDE),  '') AS DECIMAL(11,8))),
  SUM(CAST(NULLIF(TRIM(LONGITUDE), '') AS DECIMAL(11,8))),
  SUM(CASE WHEN NULLIF(TRIM(LATITUDE),  '') IS NULL
            OR NULLIF(TRIM(LONGITUDE), '') IS NULL
           THEN 0 ELSE 1 END)
FROM address_ref
WHERE NULLIF(TRIM(STREET_NAME), '')   IS NOT NULL
  AND NULLIF(TRIM(STATE), '')         IS NOT NULL
  AND NULLIF(TRIM(LOCALITY_NAME), '') IS NOT NULL
  AND COALESCE(TRIM(POSTCODE), '') REGEXP '^[0-9]{4}$'
GROUP BY 1, 2, 3, 4, 5, 6, 7;


-- -----------------------------------------------------------------------------
-- STEP 3. Collapse to one row per primary key.
--
--   Canonical form is chosen by: most addresses first, then prefer the variant
--   with a populated street_type. That second tiebreak matters -- given
--   ('HAMILTON','CR') and ('HAMILTON CR',''), you want the one where the type
--   is parsed out, because the resolver matches type separately.
-- -----------------------------------------------------------------------------

TRUNCATE TABLE locatron_street;

INSERT INTO locatron_street
  (state, locality, postcode, street_key,
   street_name, street_type, street_suffix, address_count, lat, lng)
SELECT
  v.state, v.locality, v.postcode, v.street_key,
  c.street_name, c.street_type, c.street_suffix,
  SUM(v.n),
  SUM(v.lat_sum) / NULLIF(SUM(v.geo_n), 0),
  SUM(v.lng_sum) / NULLIF(SUM(v.geo_n), 0)
FROM _ls_variant v
JOIN (
  SELECT state, locality, postcode, street_key,
         street_name, street_type, street_suffix
  FROM (
    SELECT s.*,
           ROW_NUMBER() OVER (
             PARTITION BY state, locality, postcode, street_key
             ORDER BY n DESC,
                      CASE WHEN street_type = '' THEN 1 ELSE 0 END,
                      CHAR_LENGTH(street_type) DESC,
                      street_name
           ) AS rn
    FROM _ls_variant s
  ) ranked
  WHERE rn = 1
) c
  ON  c.state      = v.state
  AND c.locality   = v.locality
  AND c.postcode   = v.postcode
  AND c.street_key = v.street_key
GROUP BY v.state, v.locality, v.postcode, v.street_key,
         c.street_name, c.street_type, c.street_suffix;


-- -----------------------------------------------------------------------------
-- STEP 4. Verification.
-- -----------------------------------------------------------------------------

-- 4a. No addresses lost between the raw table and the gazetteer.
SELECT
  (SELECT SUM(address_count) FROM locatron_street) AS gazetteer_addresses,
  (SELECT COUNT(*) FROM address_ref
     WHERE NULLIF(TRIM(STREET_NAME), '')   IS NOT NULL
       AND NULLIF(TRIM(STATE), '')         IS NOT NULL
       AND NULLIF(TRIM(LOCALITY_NAME), '') IS NOT NULL
       AND COALESCE(TRIM(POSTCODE), '') REGEXP '^[0-9]{4}$'
  ) AS source_addresses;

-- 4b. Row counts.
SELECT COUNT(*) AS street_rows,
       COUNT(DISTINCT CONCAT_WS('|', state, locality, postcode)) AS localities,
       SUM(lat IS NULL) AS rows_without_geometry
FROM locatron_street;

-- 4c. Centroids should be inside Australia, not near (0,0). If this returns
--     anything, the blank-coordinate cast is still leaking somewhere.
SELECT COUNT(*) AS out_of_bounds
FROM locatron_street
WHERE state <> 'OT'
  AND lat IS NOT NULL
  AND (lat NOT BETWEEN -44 AND -9 OR lng NOT BETWEEN 112 AND 154);

-- 4d. The row that previously failed.
SELECT * FROM locatron_street
WHERE state = 'NSW' AND locality = 'RYDE' AND postcode = '2112'
  AND street_key = 'HAMILTON CR';

-- 4e. How many keys had their variants merged. Useful to eyeball once; if it is
--     unexpectedly large, the source data may have a systematic parsing issue.
SELECT COUNT(*) AS merged_keys
FROM (
  SELECT state, locality, postcode, street_key
  FROM _ls_variant
  GROUP BY 1, 2, 3, 4
  HAVING COUNT(*) > 1
) m;


-- -----------------------------------------------------------------------------
-- STEP 5. Cleanup.
-- -----------------------------------------------------------------------------

-- Keep _ls_variant until you have reviewed 4e; it is the only record of which
-- decompositions were discarded.
-- DROP TABLE IF EXISTS _ls_variant;


-- =============================================================================
-- OPTIONAL: preserve the discarded decompositions as street aliases.
--
-- If ('HAMILTON CR','','') was the losing variant, a user typing "Hamilton Cr"
-- still normalises to the same street_key, so nothing is lost for matching.
-- But if the losing variant produced a DIFFERENT key -- which happens when the
-- suffix moves between columns -- you want it as an alias. Add a
-- locatron_street_alias table on the same pattern as locatron_locality_alias
-- and seed it from _ls_variant rows that did not win their partition.
-- =============================================================================
