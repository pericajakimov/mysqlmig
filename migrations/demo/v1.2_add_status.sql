-- v1.2_add_status.sql
-- Schema change: add a status column to orders.

ALTER TABLE orders
    ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'pending';

UPDATE orders SET status = 'paid' WHERE total_cents > 0;
