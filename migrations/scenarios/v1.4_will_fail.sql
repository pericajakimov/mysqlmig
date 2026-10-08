-- v1.4_will_fail.sql
-- FAILURE SCENARIO.
-- Copy this into migrations/demo/ (after v1.3) and run `mysqlmig run`.
-- The second statement references a column that does not exist, so the mysql
-- client returns a non-zero status. mysqlmig records v1.4 as FAILED, stops,
-- and does not run anything after it.
--
-- Note: the first statement succeeds before the failure, so this also lets you
-- see a partially-applied migration (the DBA must resolve it before retry).

CREATE TABLE audit_log (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    note VARCHAR(100) NOT NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- This column does not exist -> ERROR 1054.
UPDATE orders SET nonexistent_column = 1;
