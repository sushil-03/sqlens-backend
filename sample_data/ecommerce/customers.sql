CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    city TEXT,
    signup_date TEXT NOT NULL
);

INSERT INTO customers (id, name, email, city, signup_date) VALUES
    (1, 'Ava Thompson', 'ava.thompson@example.com', 'Seattle', '2024-01-12'),
    (2, 'Liam Brooks', 'liam.brooks@example.com', 'Austin', '2024-01-18'),
    (3, 'Noah Patel', 'noah.patel@example.com', 'Chicago', '2024-02-02'),
    (4, 'Emma Garcia', 'emma.garcia@example.com', 'Denver', '2024-02-10'),
    (5, 'Olivia Chen', 'olivia.chen@example.com', 'Seattle', '2024-02-21'),
    (6, 'Mason Wright', 'mason.wright@example.com', 'Austin', '2024-03-03'),
    (7, 'Sophia Nguyen', 'sophia.nguyen@example.com', 'Miami', '2024-03-15'),
    (8, 'Ethan Rivera', 'ethan.rivera@example.com', 'Denver', '2024-03-29'),
    (9, 'Isabella Kim', 'isabella.kim@example.com', 'Chicago', '2024-04-04'),
    (10, 'James Foster', 'james.foster@example.com', 'Miami', '2024-04-19');
