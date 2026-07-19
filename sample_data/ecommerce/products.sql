CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    price REAL NOT NULL
);

INSERT INTO products (id, name, category, price) VALUES
    (1, 'Wireless Mouse', 'Electronics', 24.99),
    (2, 'Mechanical Keyboard', 'Electronics', 89.99),
    (3, 'USB-C Hub', 'Electronics', 34.50),
    (4, 'Standing Desk Mat', 'Furniture', 59.00),
    (5, 'Office Chair', 'Furniture', 189.99),
    (6, 'Desk Lamp', 'Furniture', 42.00),
    (7, 'Notebook Set', 'Stationery', 12.99),
    (8, 'Fountain Pen', 'Stationery', 28.00),
    (9, 'Sticky Notes Pack', 'Stationery', 6.50),
    (10, 'Water Bottle', 'Lifestyle', 18.00),
    (11, 'Yoga Mat', 'Lifestyle', 32.99),
    (12, 'Backpack', 'Lifestyle', 74.99);
