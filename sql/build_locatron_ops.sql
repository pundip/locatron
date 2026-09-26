-- =============================================================================
-- build_locatron_ops.sql
-- Operational tables: unresolved-input capture and API keys.
--
-- Unlike the gazetteer builds, these are created once and never rebuilt.
-- Their contents are accumulated state, not derived data.
-- =============================================================================

USE ReferenceDB;


-- -----------------------------------------------------------------------------
-- locatron_unresolved
--
-- Every input that failed to resolve, or resolved below a confidence floor.
-- Reviewing this weekly and promoting recurring entries into
-- locatron_locality_alias is what makes the resolver good over months. It is
-- the highest-value feedback loop in the project and the easiest to skip.
--
-- Deliberately stores only the normalised string, one representative raw form,
-- and a count. No source record identifier, no foreign key to wherever the
-- string came from. Otherwise this quietly becomes a second copy of the
-- scraped dataset.
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS locatron_unresolved (
  norm_key         VARCHAR(255)  NOT NULL,
  norm_version     VARCHAR(8)    NOT NULL,

  sample_raw       VARCHAR(255)  NOT NULL,   -- one representative original form
  hit_count        INT UNSIGNED  NOT NULL DEFAULT 1,

  -- Best attempt, so you can tell "no idea at all" from "got to country only".
  best_granularity VARCHAR(16)   NULL,
  best_confidence  DECIMAL(4,3)  NULL,
  match_method     VARCHAR(32)   NULL,

  first_seen       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen        DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,

  -- Review workflow. Set reviewed=1 once you have either added an alias or
  -- decided the string is genuinely unresolvable, so it stops reappearing at
  -- the top of your queue.
  reviewed         TINYINT(1)    NOT NULL DEFAULT 0,
  resolution_note  VARCHAR(255)  NULL,

  PRIMARY KEY (norm_key, norm_version),
  KEY ix_lu_triage (reviewed, hit_count),
  KEY ix_lu_recent (last_seen)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Upsert pattern for the application:
--
--   INSERT INTO locatron_unresolved
--     (norm_key, norm_version, sample_raw, best_granularity,
--      best_confidence, match_method)
--   VALUES (:nk, :nv, :raw, :gran, :conf, :method)
--   ON DUPLICATE KEY UPDATE
--     hit_count        = hit_count + 1,
--     best_granularity = IF(VALUES(best_confidence) > COALESCE(best_confidence, -1),
--                           VALUES(best_granularity), best_granularity),
--     best_confidence  = GREATEST(COALESCE(best_confidence, 0), VALUES(best_confidence)),
--     match_method     = IF(VALUES(best_confidence) > COALESCE(best_confidence, -1),
--                           VALUES(match_method), match_method);
--
-- Write this asynchronously or fire-and-forget. A resolve request must never
-- fail because the feedback table was unavailable.


-- Weekly triage query.
--
--   SELECT norm_key, sample_raw, hit_count, best_granularity, best_confidence
--   FROM locatron_unresolved
--   WHERE reviewed = 0
--   ORDER BY hit_count DESC
--   LIMIT 50;


-- -----------------------------------------------------------------------------
-- locatron_api_key
--
-- Identity, primarily. Access control is a side benefit. The real value is
-- being able to answer "is this traffic spike the Databricks job or a runaway
-- script" and "is that p99 everyone or just batch".
--
-- Key format issued to the caller:   ltk_<key_id>_<secret>
--   key_id   short public prefix, appears in every log line
--   secret   32 bytes of urandom, hex encoded, shown once at issue and never
--            stored
--
-- key_hash is an unsalted SHA-256 of the secret. Unsalted is correct here:
-- the secret is high-entropy random, not a user-chosen password, so there is
-- no dictionary to defend against and per-request bcrypt would only add
-- latency.
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS locatron_api_key (
  key_id             VARCHAR(16)   NOT NULL,   -- public, safe to log
  key_hash           CHAR(64)      NOT NULL,   -- sha256 hex of the secret
  label              VARCHAR(64)   NOT NULL,   -- 'databricks-nightly', 'maneesh-dev'

  -- Comma-separated: resolve, batch, export, admin
  scopes             VARCHAR(255)  NOT NULL DEFAULT 'resolve',
  rate_limit_per_min INT UNSIGNED  NOT NULL DEFAULT 600,

  active             TINYINT(1)    NOT NULL DEFAULT 1,
  created_at         DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at         DATETIME      NULL,
  last_used_at       DATETIME      NULL,
  note               VARCHAR(255)  NULL,

  PRIMARY KEY (key_id),
  UNIQUE KEY uq_lak_hash (key_hash),
  KEY ix_lak_active (active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Never UPDATE last_used_at on every request -- that is a write per read and
-- it will dominate your database load. Update it at most once a minute per
-- key, or keep it in Redis and flush periodically.

-- Revoke rather than delete, so old log lines still resolve to a label:
--   UPDATE locatron_api_key SET active = 0, note = 'rotated 2026-09-11'
--   WHERE key_id = 'k_a1b2c3d4';


-- =============================================================================
-- Planned CLI surface (session 4, no web UI):
--
--   locatron keys issue --label databricks-nightly --scopes resolve,export
--   locatron keys list
--   locatron keys revoke k_a1b2c3d4
--   locatron unresolved --top 50
--   locatron unresolved alias "GREATER DANDENONG" --to "DANDENONG" --state VIC
--
-- That last one closes the feedback loop directly: read the triage queue,
-- write the alias, mark reviewed, all without leaving the terminal.
-- =============================================================================
