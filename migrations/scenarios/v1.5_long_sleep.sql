-- v1.5_long_sleep.sql
-- HANG / INTERRUPTION SCENARIO.
-- Copy into migrations/demo/ (after a successful v1.4) to test what happens
-- when a migration is interrupted mid-run and left IN_PROGRESS.
--
-- How to test the interruption:
--   1. Run it:   mysqlmig -D <db> --dir migrations/demo run
--      mysqlmig inserts an IN_PROGRESS row, then blocks inside SLEEP(60).
--   2. While it sleeps, kill it. Either:
--        a) Ctrl-C / `kill` the mysqlmig process, OR
--        b) kill the server-side query:
--           mysql -e "SHOW PROCESSLIST;"            -- find the SLEEP query id
--           mysql -e "KILL QUERY <id>;"             -- or KILL <id>
--   3. The schema_version row for v1.5 stays IN_PROGRESS (it was never
--      finalized). Check with:  mysqlmig -D <db> --dir migrations/demo status
--   4. A subsequent `run` treats v1.5 as a blocking migration:
--        - non-interactive: stops with an error,
--        - interactive: offers to retry it.
--
-- Tip: lower SLEEP(60) if 60s is too long for your test.

SELECT SLEEP(60);

CREATE TABLE never_created (id INT PRIMARY KEY);
