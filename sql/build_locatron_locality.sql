-- =============================================================================
-- build_locatron_locality.sql
-- Derived AU locality gazetteer for Locatron.
--
-- Sources (all read-only, never written to by this script):
--   address_ref          - G-NAF, authoritative for physical localities + geometry
--   AustralianPostcodes  - Australia Post / ABS enrichment, authoritative for
--                          PO Box postcodes and statistical area columns
--   aus_state_bucket     - existing hand-curated string variants
--
-- Produces:
--   locatron_locality        grain: (state, locality, postcode)
--   locatron_locality_alias  many-to-one: any string variant -> locality_id
--
-- IMPORTANT: norm_key / alias_norm_key are left NULL by this script. Fill them
-- with normalize_pass.py so build-time normalisation is byte-identical to
-- query-time. Do not compute them in SQL.
--
-- RUNTIME: Step 2 scans address_ref (~5 min). Step 5 self-joins address_ref on
-- ADDRESS_DETAIL_PID and needs ix_ar_pid to exist, or it will run for hours.
-- =============================================================================

USE ReferenceDB;

SET SESSION sql_mode = 'STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION';

-- Bump these together whenever normalisation or the source data changes.
SET @norm_version = '1';
SET @snapshot_id  = DATE_FORMAT(CURDATE(), '%Y-%m');


-- -----------------------------------------------------------------------------
-- STEP 0. Prerequisite index. Skip if already present.
-- -----------------------------------------------------------------------------

-- ALTER TABLE address_ref ADD INDEX ix_ar_pid (ADDRESS_DETAIL_PID);


-- -----------------------------------------------------------------------------
-- STEP 1. DDL
-- -----------------------------------------------------------------------------

DROP TABLE IF EXISTS locatron_locality_alias;
DROP TABLE IF EXISTS locatron_locality;

CREATE TABLE locatron_locality (
  locality_id     INT UNSIGNED NOT NULL AUTO_INCREMENT,

  -- normalize(locality). NULL until normalize_pass.py runs.
  norm_key        VARCHAR(96)   NULL,

  locality        VARCHAR(64)   NOT NULL,   -- display form, uppercase
  state           CHAR(3)       NOT NULL,
  postcode        CHAR(4)       NOT NULL,

  -- Locality centroid, from G-NAF address points where available.
  lat             DECIMAL(10,7) NULL,
  lng             DECIMAL(10,7) NULL,
  -- Postcode centroid, shared across every locality in the postcode.
  postcode_lat    DECIMAL(10,7) NULL,
  postcode_lng    DECIMAL(10,7) NULL,
  geo_source      ENUM('gnaf','auspost','none') NOT NULL DEFAULT 'none',

  address_count   INT UNSIGNED  NOT NULL DEFAULT 0,
  street_count    INT UNSIGNED  NOT NULL DEFAULT 0,

  in_gnaf         TINYINT(1)    NOT NULL DEFAULT 0,
  in_auspost      TINYINT(1)    NOT NULL DEFAULT 0,
  is_postal_only  TINYINT(1)    NOT NULL DEFAULT 0,  -- PO Box / mail-only
  delivery_type   VARCHAR(32)   NULL,                -- AustralianPostcodes.type

  sa2_name        VARCHAR(64)   NULL,
  sa3_name        VARCHAR(64)   NULL,
  sa4_name        VARCHAR(64)   NULL,
  lga_name        VARCHAR(64)   NULL,
  remoteness      VARCHAR(4)    NULL,                -- R1..R5

  norm_version    VARCHAR(8)    NOT NULL,
  snapshot_id     VARCHAR(32)   NOT NULL,
  built_at        DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,

  PRIMARY KEY (locality_id),
  UNIQUE KEY uq_ll_state_loc_pc (state, locality, postcode),
  KEY ix_ll_norm (norm_key),
  KEY ix_ll_norm_state (norm_key, state),
  KEY ix_ll_postcode (postcode),
  KEY ix_ll_state_postcode (state, postcode)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE locatron_locality_alias (
  alias_id       INT UNSIGNED NOT NULL AUTO_INCREMENT,

  alias_norm_key VARCHAR(96)  NULL,          -- normalize(alias_display)
  alias_display  VARCHAR(96)  NOT NULL,
  locality_id    INT UNSIGNED NOT NULL,

  alias_type     ENUM('gnaf_alias','auspost_variant','state_bucket',
                      'abbrev','manual') NOT NULL,
  -- Score multiplier applied when a match comes via this alias rather than
  -- the canonical name. Lets you keep low-trust aliases without them
  -- outranking exact hits.
  confidence     DECIMAL(3,2) NOT NULL DEFAULT 1.00,

  norm_version   VARCHAR(8)   NOT NULL,
  built_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,

  PRIMARY KEY (alias_id),
  UNIQUE KEY uq_lla_alias_target (alias_display, locality_id),
  KEY ix_lla_norm (alias_norm_key),
  KEY ix_lla_locality (locality_id),
  CONSTRAINT fk_lla_locality FOREIGN KEY (locality_id)
    REFERENCES locatron_locality (locality_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- -----------------------------------------------------------------------------
-- STEP 2. Aggregate G-NAF.
-- -----------------------------------------------------------------------------

DROP TABLE IF EXISTS _ll_gnaf;
CREATE TABLE _ll_gnaf (
  postcode      CHAR(4)       NOT NULL,
  locality      VARCHAR(64)   NOT NULL,
  state         CHAR(3)       NOT NULL,
  lat           DECIMAL(10,7) NULL,
  lng           DECIMAL(10,7) NULL,
  address_count INT UNSIGNED  NOT NULL,
  PRIMARY KEY (state, locality, postcode)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO _ll_gnaf (postcode, locality, state, lat, lng, address_count)
SELECT
  LPAD(TRIM(POSTCODE), 4, '0'),
  UPPER(TRIM(LOCALITY_NAME)),
  UPPER(TRIM(STATE)),
  AVG(CAST(NULLIF(TRIM(LATITUDE),  '') AS DECIMAL(11,8))),
  AVG(CAST(NULLIF(TRIM(LONGITUDE), '') AS DECIMAL(11,8))),
  COUNT(*)
FROM address_ref
WHERE COALESCE(TRIM(POSTCODE), '') REGEXP '^[0-9]{4}$'
  AND NULLIF(TRIM(LOCALITY_NAME), '') IS NOT NULL
  AND NULLIF(TRIM(STATE), '')         IS NOT NULL
  AND COALESCE(TRIM(ALIAS_PRINCIPAL), '') <> 'ALIAS'
GROUP BY 1, 2, 3;

-- Postcode centroid, weighted by address count.
DROP TABLE IF EXISTS _ll_gnaf_pc;
CREATE TABLE _ll_gnaf_pc (
  postcode CHAR(4)       NOT NULL,
  state    CHAR(3)       NOT NULL,
  lat      DECIMAL(10,7) NULL,
  lng      DECIMAL(10,7) NULL,
  PRIMARY KEY (state, postcode)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO _ll_gnaf_pc (postcode, state, lat, lng)
SELECT postcode, state,
       SUM(lat * address_count) / NULLIF(SUM(address_count), 0),
       SUM(lng * address_count) / NULLIF(SUM(address_count), 0)
FROM _ll_gnaf
GROUP BY postcode, state;


-- -----------------------------------------------------------------------------
-- STEP 3. Deduplicate AustralianPostcodes into a clean enrichment source.
-- -----------------------------------------------------------------------------

DROP TABLE IF EXISTS _ll_auspost;
CREATE TABLE _ll_auspost (
  postcode      CHAR(4)       NOT NULL,
  locality      VARCHAR(64)   NOT NULL,
  state         CHAR(3)       NOT NULL,
  lat           DECIMAL(10,7) NULL,
  lng           DECIMAL(10,7) NULL,
  delivery_type VARCHAR(32)   NULL,
  sa2_name      VARCHAR(64)   NULL,
  sa3_name      VARCHAR(64)   NULL,
  sa4_name      VARCHAR(64)   NULL,
  lga_name      VARCHAR(64)   NULL,
  remoteness    VARCHAR(4)    NULL,
  PRIMARY KEY (state, locality, postcode)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO _ll_auspost
SELECT k_postcode, k_locality, k_state,
       CAST(NULLIF(TRIM(Lat_precise),  '') AS DECIMAL(11,8)),
       CAST(NULLIF(TRIM(Long_precise), '') AS DECIMAL(11,8)),
       NULLIF(TRIM(type), ''),
       NULLIF(TRIM(SA2_NAME_2016), ''),
       NULLIF(TRIM(SA3_NAME_2016), ''),
       NULLIF(TRIM(SA4_NAME_2016), ''),
       NULLIF(TRIM(lgaregion), ''),
       NULLIF(TRIM(region), '')
FROM (
  SELECT LPAD(TRIM(postcode), 4, '0') AS k_postcode,
         UPPER(TRIM(locality))        AS k_locality,
         UPPER(TRIM(state))           AS k_state,
         Lat_precise, Long_precise, type,
         SA2_NAME_2016, SA3_NAME_2016, SA4_NAME_2016, lgaregion, region,
         ROW_NUMBER() OVER (
           PARTITION BY LPAD(TRIM(postcode), 4, '0'),
                        UPPER(TRIM(locality)),
                        UPPER(TRIM(state))
           ORDER BY id
         ) AS rn
  FROM AustralianPostcodes
) d
WHERE rn = 1
  AND k_postcode REGEXP '^[0-9]{4}$'
  AND k_locality <> ''
  AND k_state    <> '';


-- -----------------------------------------------------------------------------
-- STEP 4. Merge into locatron_locality.
--         Full outer join emulated as (G-NAF LEFT JOIN AusPost) UNION ALL
--         (AusPost rows with no G-NAF match).
-- -----------------------------------------------------------------------------

INSERT INTO locatron_locality
  (locality, state, postcode, lat, lng, postcode_lat, postcode_lng, geo_source,
   address_count, in_gnaf, in_auspost, is_postal_only, delivery_type,
   sa2_name, sa3_name, sa4_name, lga_name, remoteness,
   norm_version, snapshot_id)

-- 4a. Everything G-NAF knows about, enriched from Australia Post where matched.
SELECT
  g.locality, g.state, g.postcode,
  g.lat, g.lng,
  p.lat, p.lng,
  'gnaf',
  g.address_count,
  1,
  CASE WHEN a.postcode IS NULL THEN 0 ELSE 1 END,
  0,
  a.delivery_type,
  a.sa2_name, a.sa3_name, a.sa4_name, a.lga_name, a.remoteness,
  @norm_version, @snapshot_id
FROM _ll_gnaf g
LEFT JOIN _ll_gnaf_pc p
  ON p.state = g.state AND p.postcode = g.postcode
LEFT JOIN _ll_auspost a
  ON a.state = g.state AND a.locality = g.locality AND a.postcode = g.postcode

UNION ALL

-- 4b. Australia Post rows G-NAF has no equivalent for. Overwhelmingly PO Box
--     and mail-only postcodes, which G-NAF does not carry at all. These matter
--     for postal-address resolution, so they are kept and flagged.
SELECT
  a.locality, a.state, a.postcode,
  a.lat, a.lng,
  a.lat, a.lng,
  CASE WHEN a.lat IS NULL THEN 'none' ELSE 'auspost' END,
  0,
  0,
  1,
  1,
  a.delivery_type,
  a.sa2_name, a.sa3_name, a.sa4_name, a.lga_name, a.remoteness,
  @norm_version, @snapshot_id
FROM _ll_auspost a
LEFT JOIN _ll_gnaf g
  ON g.state = a.state AND g.locality = a.locality AND g.postcode = a.postcode
WHERE g.postcode IS NULL;

-- 4c. Street counts from locatron_street, if it has already been built.
UPDATE locatron_locality l
JOIN (
  SELECT state, locality, postcode, COUNT(*) AS n
  FROM locatron_street
  GROUP BY state, locality, postcode
) s
  ON s.state = l.state AND s.locality = l.locality AND s.postcode = l.postcode
SET l.street_count = s.n;


-- -----------------------------------------------------------------------------
-- STEP 5. Seed aliases.
-- -----------------------------------------------------------------------------

-- 5a. G-NAF alias localities. An address flagged ALIAS points at its principal
--     via PRINCIPAL_PID; where the two disagree on locality name, that is a real
--     alternate name for the principal locality.
--     Requires ix_ar_pid. Expect a few minutes.
INSERT IGNORE INTO locatron_locality_alias
  (alias_display, locality_id, alias_type, confidence, norm_version)
SELECT DISTINCT
  UPPER(TRIM(a.LOCALITY_NAME)),
  l.locality_id,
  'gnaf_alias',
  1.00,
  @norm_version
FROM address_ref a
JOIN address_ref p
  ON p.ADDRESS_DETAIL_PID = a.PRINCIPAL_PID
JOIN locatron_locality l
  ON l.state    = UPPER(TRIM(p.STATE))
 AND l.locality = UPPER(TRIM(p.LOCALITY_NAME))
 AND l.postcode = LPAD(TRIM(p.POSTCODE), 4, '0')
WHERE COALESCE(TRIM(a.ALIAS_PRINCIPAL), '') = 'ALIAS'
  AND NULLIF(TRIM(a.LOCALITY_NAME), '') IS NOT NULL
  AND UPPER(TRIM(a.LOCALITY_NAME)) <> UPPER(TRIM(p.LOCALITY_NAME));

-- 5b. aus_state_bucket entries of the form "<STATE> <LOCALITY>", e.g.
--     "VIC AVON PLAINS". Strip the leading state token and keep the remainder
--     if it resolves to a locality in that state.
INSERT IGNORE INTO locatron_locality_alias
  (alias_display, locality_id, alias_type, confidence, norm_version)
SELECT DISTINCT
  UPPER(TRIM(b.value)),
  l.locality_id,
  'state_bucket',
  0.95,
  @norm_version
FROM aus_state_bucket b
JOIN locatron_locality l
  ON l.state = UPPER(TRIM(b.state))
 AND l.locality = UPPER(TRIM(
       SUBSTRING(TRIM(b.value), CHAR_LENGTH(TRIM(b.state)) + 2)))
WHERE UPPER(TRIM(b.value)) LIKE CONCAT(UPPER(TRIM(b.state)), ' %')
  AND CHAR_LENGTH(TRIM(b.value)) > CHAR_LENGTH(TRIM(b.state)) + 1;

-- 5c. Australia Post locality spellings that differ from G-NAF for the same
--     postcode+state. Lower confidence: matched on postcode, not on name.
INSERT IGNORE INTO locatron_locality_alias
  (alias_display, locality_id, alias_type, confidence, norm_version)
SELECT DISTINCT
  a.locality,
  l.locality_id,
  'auspost_variant',
  0.80,
  @norm_version
FROM _ll_auspost a
JOIN locatron_locality l
  ON l.state = a.state AND l.postcode = a.postcode
WHERE l.in_gnaf = 1
  AND l.locality <> a.locality
  AND NOT EXISTS (
    SELECT 1 FROM _ll_gnaf g
    WHERE g.state = a.state AND g.locality = a.locality AND g.postcode = a.postcode
  );

-- 5d. Manual aliases. Add your own here; alias_type='manual' survives rebuilds
--     if you export and reload them (see note at the end of this file).
--     Example shape:
-- INSERT IGNORE INTO locatron_locality_alias
--   (alias_display, locality_id, alias_type, confidence, norm_version)
-- SELECT 'ST KILDA BEACH', locality_id, 'manual', 1.00, @norm_version
-- FROM locatron_locality WHERE locality = 'ST KILDA' AND state = 'VIC';


-- -----------------------------------------------------------------------------
-- STEP 6. Verification. Run before pointing the resolver at this.
-- -----------------------------------------------------------------------------

SELECT COUNT(*) AS total,
       SUM(in_gnaf)        AS from_gnaf,
       SUM(in_auspost)     AS from_auspost,
       SUM(is_postal_only) AS postal_only,
       SUM(geo_source = 'none') AS no_geometry
FROM locatron_locality;

-- Locality names that are ambiguous across states. These are the ones your
-- resolver has to disambiguate; address_count is the natural tiebreak.
SELECT locality, COUNT(DISTINCT state) AS states,
       GROUP_CONCAT(DISTINCT state ORDER BY state) AS state_list
FROM locatron_locality
GROUP BY locality
HAVING states > 1
ORDER BY states DESC, locality
LIMIT 40;

-- Geometry sanity. Australia is roughly lat -44..-9, lng 112..154.
SELECT COUNT(*) AS out_of_bounds
FROM locatron_locality
WHERE state <> 'OT'
  AND lat IS NOT NULL
  AND (lat NOT BETWEEN -44 AND -9 OR lng NOT BETWEEN 112 AND 154);

-- Aliases that collide, i.e. one string pointing at more than one locality.
-- Not necessarily wrong, but the resolver needs a tiebreak for these.
SELECT alias_display, COUNT(*) AS targets
FROM locatron_locality_alias
GROUP BY alias_display
HAVING targets > 1
ORDER BY targets DESC
LIMIT 40;

-- Spot check.
SELECT locality_id, locality, state, postcode, lat, lng,
       address_count, street_count, in_gnaf, in_auspost, is_postal_only
FROM locatron_locality
WHERE locality IN ('CARRUM DOWNS','PARRAMATTA','WINNELLIE','WORLD SQUARE')
ORDER BY state, postcode;


-- -----------------------------------------------------------------------------
-- STEP 7. Cleanup.
-- -----------------------------------------------------------------------------

DROP TABLE IF EXISTS _ll_gnaf;
DROP TABLE IF EXISTS _ll_gnaf_pc;
DROP TABLE IF EXISTS _ll_auspost;


-- =============================================================================
-- NEXT: run normalize_pass.py to populate norm_key and alias_norm_key,
-- then dedupe_locality.py. Until the first runs, every ix_ll_norm lookup
-- returns nothing.
--
-- dedupe_locality.py collapses localities that differ only in punctuation.
-- The unique index below is on the raw locality, so G-NAF's D'AGUILAR (0x27)
-- and AusPost's D’AGUILAR (U+2019) are two rows here and one place after
-- normalisation. 16 groups are affected; nine are that apostrophe, three are
-- 'NO. 4 BRANCH' against 'NO 4 BRANCH', and four are the same pair repeated
-- in a second state.
-- Do not try to fold that here: normalisation belongs in normalize.py alone.
--
-- ON REBUILDS: Step 1 drops both tables, which takes your manual aliases with
-- them. Export first:
--
--   SELECT a.alias_display, l.locality, l.state, l.postcode, a.confidence
--   FROM locatron_locality_alias a
--   JOIN locatron_locality l USING (locality_id)
--   WHERE a.alias_type = 'manual';
--
-- Better still, keep manual aliases in a separate hand-maintained table that
-- this script never drops, and re-apply them as a final step.
-- =============================================================================
