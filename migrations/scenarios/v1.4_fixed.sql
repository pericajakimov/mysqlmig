-- v1.4_fixed.sql
-- RETRY SCENARIO (the fix for v1.4_will_fail.sql).
-- After v1.4 is recorded FAILED, replace the file in migrations/demo/ with this
-- corrected version (keep the SAME version v1.4), then run interactively:
--     mysqlmig -D <db> --dir migrations/demo
--     choose Run, confirm the retry prompt.
-- mysqlmig updates the existing v1.4 row FAILED -> SUCCESS in place and appends
-- the retry output to the existing execution_log.
--
-- Because v1.4_will_fail.sql already created audit_log, we use IF NOT EXISTS so
-- the retry is safe to run against the partially-applied state.

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    note VARCHAR(100) NOT NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO audit_log (note) VALUES ('v1.4 applied successfully on retry');
