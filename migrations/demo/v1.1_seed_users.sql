-- v1.1_seed_users.sql
-- Insert some seed data. Demonstrates multiple statements in one migration.

INSERT INTO users (email) VALUES
    ('alice@example.com'),
    ('bob@example.com'),
    ('carol@example.com');

INSERT INTO orders (user_id, total_cents)
SELECT id, 1000 FROM users WHERE email = 'alice@example.com';
