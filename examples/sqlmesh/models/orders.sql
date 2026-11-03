MODEL (
  name memory.analytics.orders,
  kind FULL,
  description 'Curated orders.',
  columns (id INT, amount DECIMAL(10, 2)),
  column_descriptions (id = 'Order identifier'),
  audits (not_null(columns := (id)))
);

SELECT id, amount FROM memory.raw.raw_orders;
