# Safe Grid Bot Capital Scaling Process

## 🛡️ Safety Guarantees

The scaling function is bulletproof:

```
✅ Does NOT disable any branches
✅ Does NOT modify grid bot logic or parameters  
✅ Does NOT stop grid bot execution
✅ Grid bot continues trading while scaling happens
✅ All changes are atomic (all-or-nothing)
✅ Full rollback on any error
```

**Key point**: Scaling ONLY modifies the `allocated_usd` field. Nothing else changes.

---

## 📋 Safe Scaling Workflow (3 Steps)

### Step 1: Preview Changes (DRY RUN) - **DO THIS FIRST**
```bash
cd /home/user/empire-v2
python scale_grid_capital.py 1.25
```

**Expected output:**
```
Grid Bot Capital Scaling Utility
=============================================
Scale factor: 1.25x (25% increase)
Mode: DRY RUN (preview only)
=============================================

📋 DRY RUN PREVIEW (No changes applied)
Branches to scale: 3
Old total allocation: $300.00
New total allocation: $375.00
Total increase: $75.00

Per-Branch Updates:
  crypto_grid_1 (DOGE-USD) [✅ ACTIVE]: $100.00 → $125.00 (+25.0%)
  crypto_grid_2 (STX-USD) [✅ ACTIVE]: $120.00 → $150.00 (+25.0%)
  crypto_grid_3 (ETH-USD) [✅ ACTIVE]: $80.00 → $100.00 (+25.0%)

=============================================
🔍 DRY RUN COMPLETE - Preview looks good?

To EXECUTE this scaling, run:
  python scale_grid_capital.py 1.25 --confirm

SAFETY CHECKS PASSED:
  ✅ All branches remain ACTIVE
  ✅ Grid bot will NOT be disabled
  ✅ Only allocated_usd field will change
  ✅ Grid bot picks up new allocations on next cycle
=============================================
```

**What to verify:**
- All branches show `[✅ ACTIVE]`
- No branches are `[⚠️  DISABLED]`
- The allocation increases look reasonable
- Total increase matches scale factor

### Step 2: Review and Confirm
Look at the preview output and verify:
- ✅ Grid bot is still running (check another terminal)
- ✅ All branches are active
- ✅ The increase amounts are acceptable
- ✅ No unexpected changes

### Step 3: Execute (IF PREVIEW LOOKS GOOD)
```bash
python scale_grid_capital.py 1.25 --confirm
```

**Expected output:**
```
=============================================
✅ SCALING EXECUTED SUCCESSFULLY
Branches to scale: 3
Old total allocation: $300.00
New total allocation: $375.00
Total increase: $75.00

Per-Branch Updates:
  crypto_grid_1 (DOGE-USD) [✅ ACTIVE]: $100.00 → $125.00 (+25.0%)
  crypto_grid_2 (STX-USD) [✅ ACTIVE]: $120.00 → $150.00 (+25.0%)
  crypto_grid_3 (ETH-USD) [✅ ACTIVE]: $80.00 → $100.00 (+25.0%)

=============================================
SAFETY VERIFICATION:
  ✅ Grid bot remains ACTIVE
  ✅ No branches were disabled
  ✅ Only allocated_usd field was modified
  ✅ Grid bot picks up new allocations on next cycle (~30 sec)
  ✅ All changes logged to activity feed
=============================================
Grid bot will use new allocations on next cycle (~30 seconds).
=============================================
```

---

## What Happens Next (Timeline)

### Immediately (During Scaling)
- Database transaction updates `allocated_usd` for each branch
- Grid bot continues running (doesn't know about changes yet)
- All active branches remain `active=true`

### Next 30 seconds
- Grid bot completes its current cycle
- On next cycle start, it queries database fresh
- It picks up the new `allocated_usd` values
- New slice sizing reflects updated allocations

### Next cycle (60 seconds out)
- First order placed with new position sizing
- Dashboard shows updated allocations
- Activity feed logs the scaling event

---

## Scale Factors Explained

| Factor | Increase | Example: $300 → |
|--------|----------|---|
| 1.10 | 10% | $330 |
| 1.25 | 25% | $375 |
| 1.50 | 50% | $450 |
| 2.00 | 100% | $600 |

**Recommendations:**
- Start conservative: 1.10 or 1.25 (10-25%)
- After first week of success: 1.50 (50%)
- Only extreme growth: 2.00+ (double)

---

## Safety Checks Built In

### 1. Pre-Scaling Validation
```python
if scale_factor <= 1.0:
    return {"error": "scale_factor must be > 1.0", "status": "failed"}
```
❌ Prevents accidental downsizing

### 2. Branch Activity Check
```python
result = await db.execute(
    select(CryptoGridBranch).where(CryptoGridBranch.active == True)
)
```
✅ Only touches active branches

### 3. Per-Branch Safety Loop
```python
if not branch.active:
    continue  # Skip any disabled branches
if old_usd <= 0:
    continue  # Skip zero-allocation branches
```
✅ Skips invalid branches automatically

### 4. Single Field Modification
```python
branch.allocated_usd = new_usd  # ONLY this field
# Never touches: active, product_id, bot_name, etc.
```
✅ Surgical change, nothing else affected

### 5. Atomic Transaction
```python
try:
    await db.commit()
except Exception as e:
    await db.rollback()  # All-or-nothing
    return {"error": str(e), "status": "failed"}
```
✅ Either all changes apply or none do

---

## Troubleshooting

### "No active grid branches to scale"
**Means**: No branches are active in database  
**Check**: Are grid branches actually running?
```bash
# From another terminal, check grid bot is still running
ps aux | grep crypto_grid_bot
```

### All branches show `[⚠️  DISABLED]`
**Means**: Grid bot branches were disabled  
**Action**: DO NOT CONTINUE - check grid bot status first

### "No valid branches to scale (all inactive or zero allocation)"
**Means**: No branches have positive allocation  
**Action**: Check database to see branch state

### Grid bot stops during scaling
**Should NOT happen** with new safety checks  
**If it does**: Check logs for database errors
```bash
# Check if grid bot is still running
ps aux | grep crypto_grid_bot
# Check database connection
echo "SELECT COUNT(*) FROM crypto_grid_branch WHERE active=true;" | psql -U user
```

---

## Verification After Scaling

### Terminal 1: Verify Grid Bot Still Running
```bash
# Should see [GRID] log lines every 30-60 seconds
# Should NOT see any errors or disabled messages
tail -f /path/to/grid/bot/logs
```

### Terminal 2: Verify New Allocations in Database
```bash
python -c "
import asyncio
import crypto_grid_bot
status = asyncio.run(crypto_grid_bot.get_grid_status())
print('Grid Bot Status:')
print(f'  Branches: {status[\"branch_count\"]}')
print(f'  Total allocation: \${status[\"total_allocated_usd\"]:,.2f}')
for branch in status['branches'][:5]:
    print(f'    {branch[\"bot_name\"]}: \${branch[\"allocated_usd\"]:,.2f}')
"
```

### Terminal 3: Verify Dashboard Updates
```bash
# Open dashboard - should show new allocations within 30-60 seconds
cd /home/user/Delfina
python scripts/dashboard_server.py
# Visit: http://localhost:8080/dashboard
```

---

## Complete Example Walkthrough

### Starting State
- Grid bot running with 3 branches
- Current total allocation: $300
- All branches active and trading

### Step 1: Preview
```bash
$ python scale_grid_capital.py 1.25
# ... dry run output shows $300 → $375
```
✅ Looks good, proceed

### Step 2: Execute
```bash
$ python scale_grid_capital.py 1.25 --confirm
# ... execution output shows success
```
✅ Changes applied atomically

### Step 3: Verify (30 seconds later)
```bash
$ python -c "import asyncio, crypto_grid_bot; \
  status = asyncio.run(crypto_grid_bot.get_grid_status()); \
  print(f\"Total: \${status['total_allocated_usd']:,.2f}\")"
# Output: Total: $375.00
```
✅ Database confirmed

### Step 4: Monitor Dashboard
- Open http://localhost:8080/dashboard
- Grid branches now show $375 total
- New slice sizes visible in recent trades
✅ Live trading with new allocations

---

## Key Takeaways

1. **Always use DRY RUN first** - preview before executing
2. **Grid bot never stops** - scaling happens safely in background
3. **All-or-nothing transaction** - errors roll back completely
4. **Only `allocated_usd` changes** - no other fields touched
5. **Dashboard shows progress** - verify success in real-time

---

## Commands Cheat Sheet

```bash
# Preview scaling (no changes)
python scale_grid_capital.py 1.25

# Execute scaling (apply changes)
python scale_grid_capital.py 1.25 --confirm

# Check current allocations
python -c "
import asyncio, crypto_grid_bot
status = asyncio.run(crypto_grid_bot.get_grid_status())
for b in status['branches']:
    print(f'{b[\"bot_name\"]}: \${b[\"allocated_usd\"]:,.2f}')
"

# View scaling in activity feed
# In dashboard: Grid Bot section → Activity Log
```

---

**Remember**: Scaling is safe. Always preview first. Grid bot keeps running.
