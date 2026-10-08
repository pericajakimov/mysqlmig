-- v1.3_stored_procedure.sql
-- Demonstrates DELIMITER handling and comments inside a stored program body
-- (why mysqlmig always passes --comments and runs the file via stdin).

DELIMITER //

CREATE PROCEDURE order_count_for_user(IN p_user_id BIGINT UNSIGNED)
BEGIN
    -- Return the number of orders for the given user.
    SELECT COUNT(*) AS order_count
    FROM orders
    WHERE user_id = p_user_id;
END //

DELIMITER ;
