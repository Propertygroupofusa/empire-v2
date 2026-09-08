-- Migration: Reallocate Grid Capital to $1,080.60 Across 5 Branches
-- Date: 2026-09-08
-- Purpose: Increase capital allocation weighted by recent performance
-- BTC-USD remains paused (active=0)

-- Allocation changes (weighted by performance):
-- ETH-USD: $277.71 → $369.95 (+$92.24, 40% of increase)
-- STX-USD: $146.34 → $203.99 (+$57.65, 25% of increase)
-- DOGE-USD: $135.81 → $181.93 (+$46.12, 20% of increase)
-- AAVE-USD: $124.35 → $158.94 (+$34.59, 15% of increase)
-- BTC-USD: $165.79 → $165.79 (PAUSED, no change)
-- TOTAL: $850.00 → $1,080.60 (+$230.60)

UPDATE crypto_grid_branches
SET allocated_usd = 369.95, updated_at = CURRENT_TIMESTAMP
WHERE product_id = 'ETH-USD';

UPDATE crypto_grid_branches
SET allocated_usd = 203.99, updated_at = CURRENT_TIMESTAMP
WHERE product_id = 'STX-USD';

UPDATE crypto_grid_branches
SET allocated_usd = 181.93, updated_at = CURRENT_TIMESTAMP
WHERE product_id = 'DOGE-USD';

UPDATE crypto_grid_branches
SET allocated_usd = 158.94, updated_at = CURRENT_TIMESTAMP
WHERE product_id = 'AAVE-USD';

-- BTC-USD remains paused with same allocation
UPDATE crypto_grid_branches
SET active = 0, updated_at = CURRENT_TIMESTAMP
WHERE product_id = 'BTC-USD';

-- Verify totals
-- SELECT SUM(allocated_usd) as total_allocated FROM crypto_grid_branches;
-- Expected: 1080.60
