-- =============================================================================
-- grants.sql
-- MySQL accounts for Locatron. Run once, as an admin account.
--
-- CLAUDE.md says upstream tables are read-only. That is a convention, and
-- conventions get violated by tired humans and enthusiastic agents alike.
-- These grants make it an enforced property instead.
--
-- Three accounts, by what they are allowed to break:
--
--   locatron_ro     SELECT only, anywhere. Used by PyCharm, by Claude Code,
--                   and for ad-hoc querying. Cannot damage anything.
--
--   locatron        The running service. SELECT everywhere, but writes only to
--                   the two tables that accumulate runtime state. Cannot touch
--                   the gazetteer or any upstream table.
--
--   locatron_build  Gazetteer rebuilds. Can CREATE, DROP, and TRUNCATE the
--                   derived tables. Still cannot write to upstream. Used
--                   manually when running sql/build_*.sql, never by a service.
--
-- Generate each password with: openssl rand -base64 24
-- =============================================================================

-- Replace with your container's address, or '%' if you prefer. Narrower is
-- better; '%' means the account works from anywhere that can reach port 3335.
SET @host = '%';


-- -----------------------------------------------------------------------------
-- 1. Read-only. This is the one that goes in PyCharm and in any agent's reach.
-- -----------------------------------------------------------------------------

CREATE USER IF NOT EXISTS 'locatron_ro'@'%' IDENTIFIED BY 'CHANGE_ME_RO';
GRANT SELECT ON ReferenceDB.* TO 'locatron_ro'@'%';


-- -----------------------------------------------------------------------------
-- 2. Service account. Reads everything, writes only runtime state.
--
-- MySQL does not accept wildcards in table names for GRANT, so these are
-- listed explicitly. Add a line here if you add a table the service writes to.
-- -----------------------------------------------------------------------------

CREATE USER IF NOT EXISTS 'locatron'@'%' IDENTIFIED BY 'CHANGE_ME_APP';

GRANT SELECT ON ReferenceDB.* TO 'locatron'@'%';

GRANT INSERT, UPDATE ON ReferenceDB.locatron_unresolved TO 'locatron'@'%';
GRANT UPDATE          ON ReferenceDB.locatron_api_key   TO 'locatron'@'%';

-- Deliberately absent: any write on address_ref, AustralianPostcodes, Cities,
-- Countries, aus_state_bucket, country_bucket, locatron_locality,
-- locatron_locality_alias, locatron_street. If the service ever appears to
-- need one of these, that is a design problem, not a grant problem.


-- -----------------------------------------------------------------------------
-- 3. Build account. Used by hand when running the gazetteer rebuilds.
-- -----------------------------------------------------------------------------

CREATE USER IF NOT EXISTS 'locatron_build'@'%' IDENTIFIED BY 'CHANGE_ME_BUILD';

GRANT SELECT ON ReferenceDB.* TO 'locatron_build'@'%';

GRANT ALL PRIVILEGES ON ReferenceDB.locatron_street          TO 'locatron_build'@'%';
GRANT ALL PRIVILEGES ON ReferenceDB.locatron_locality        TO 'locatron_build'@'%';
GRANT ALL PRIVILEGES ON ReferenceDB.locatron_locality_alias  TO 'locatron_build'@'%';
GRANT ALL PRIVILEGES ON ReferenceDB.locatron_unresolved      TO 'locatron_build'@'%';
GRANT ALL PRIVILEGES ON ReferenceDB.locatron_api_key         TO 'locatron_build'@'%';

-- The build scripts also create temporary staging tables (_ls_variant,
-- _ll_gnaf, and so on). CREATE at database scope is needed for those, which
-- unavoidably also permits creating other tables in ReferenceDB. It does not
-- permit modifying existing upstream data.
GRANT CREATE, DROP ON ReferenceDB.* TO 'locatron_build'@'%';

FLUSH PRIVILEGES;


-- -----------------------------------------------------------------------------
-- Verify
-- -----------------------------------------------------------------------------

SHOW GRANTS FOR 'locatron_ro'@'%';
SHOW GRANTS FOR 'locatron'@'%';
SHOW GRANTS FOR 'locatron_build'@'%';

-- Prove the service account cannot damage the gazetteer. Connected as
-- 'locatron', this must fail with error 1142:
--
--   DELETE FROM ReferenceDB.locatron_locality LIMIT 1;
--   UPDATE ReferenceDB.address_ref SET STATE = 'XX' LIMIT 1;
--
-- If either succeeds, the grants did not apply. Check for a wildcard grant
-- from an earlier setup:
--
--   SELECT user, host FROM mysql.user WHERE user LIKE 'locatron%';
