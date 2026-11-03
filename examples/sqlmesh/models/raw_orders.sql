MODEL (
  name memory.raw.raw_orders,
  kind FULL,
  description 'Example source orders.',
  columns (id INT, amount DECIMAL(10, 2)),
  column_descriptions (id = 'Order identifier', amount = 'Gross order amount')
);

SELECT 1::INT AS id, 10.00::DECIMAL(10, 2) AS amount
UNION ALL
SELECT 2::INT AS id, 15.00::DECIMAL(10, 2) AS amount;
