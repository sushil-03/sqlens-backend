CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    order_date TEXT NOT NULL,
    status TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE TABLE order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    unit_price REAL NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (product_id) REFERENCES products(id)
);

INSERT INTO orders (id, customer_id, order_date, status) VALUES
    (1, 1, '2024-02-01', 'delivered'),
    (2, 2, '2024-02-05', 'delivered'),
    (3, 1, '2024-02-20', 'delivered'),
    (4, 3, '2024-03-01', 'shipped'),
    (5, 4, '2024-03-04', 'delivered'),
    (6, 5, '2024-03-10', 'delivered'),
    (7, 6, '2024-03-18', 'cancelled'),
    (8, 7, '2024-03-22', 'delivered'),
    (9, 2, '2024-04-01', 'shipped'),
    (10, 8, '2024-04-05', 'delivered'),
    (11, 9, '2024-04-12', 'processing'),
    (12, 10, '2024-04-20', 'delivered'),
    (13, 5, '2024-04-25', 'delivered'),
    (14, 3, '2024-05-02', 'delivered'),
    (15, 1, '2024-05-10', 'processing');

INSERT INTO order_items (id, order_id, product_id, quantity, unit_price) VALUES
    (1, 1, 1, 2, 24.99),
    (2, 1, 3, 1, 34.50),
    (3, 2, 2, 1, 89.99),
    (4, 3, 5, 1, 189.99),
    (5, 4, 7, 3, 12.99),
    (6, 5, 4, 1, 59.00),
    (7, 5, 6, 1, 42.00),
    (8, 6, 9, 5, 6.50),
    (9, 7, 11, 1, 32.99),
    (10, 8, 12, 1, 74.99),
    (11, 9, 10, 2, 18.00),
    (12, 10, 2, 1, 89.99),
    (13, 10, 3, 2, 34.50),
    (14, 11, 8, 1, 28.00),
    (15, 12, 1, 1, 24.99),
    (16, 13, 5, 1, 189.99),
    (17, 13, 6, 2, 42.00),
    (18, 14, 7, 2, 12.99),
    (19, 15, 11, 1, 32.99),
    (20, 15, 10, 1, 18.00);
