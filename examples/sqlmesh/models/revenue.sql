MODEL (
  name memory.analytics.revenue,
  kind FULL,
  description 'Total order revenue.',
  columns (total DECIMAL(18, 2))
);

SELECT SUM(amount)::DECIMAL(18, 2) AS total FROM memory.analytics.orders;
