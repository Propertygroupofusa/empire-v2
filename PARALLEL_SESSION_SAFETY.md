# Parallel Session Safety

## Critical Issue: Silent Reversion of Merged Work

**Problem:** When multiple Claude sessions work on the same repo simultaneously, one session can push to main and silently revert PRs merged by another session.

**Example from today:**
- Session A merged #140 (referral table split)
- Session B pushed to main, reverting #140
- Session A had to re-merge the work as #143

**Root cause:** Sessions don't coordinate on what's already merged to main. Each session works from its local knowledge, not the actual HEAD of origin/main.

---

## Prevention: Pre-Push Hook

A pre-push hook now guards against this:

```bash
.git/hooks/pre-push
```

**What it does:**
1. **Checks if your local main is behind origin/main** (other sessions merged work)
   - If yes: BLOCKS push, requires `git fetch origin main && git merge origin/main`
   - Forces you to integrate remote changes before pushing
2. **Warns before pushing multiple commits to main**
   - Lists all commits that will be pushed
   - Requires manual confirmation
3. **Only runs on main branch** (other branches unaffected)

**Behavior:**
```bash
git push origin main
# If behind: ❌ ERROR: Your main is behind origin/main by 2 commits
# If ahead: ⚠️ WARNING: Pushing 3 commits to main
#           [lists commits]
#           Continue? (y/N)
```

---

## What You Must Do When Pushing to Main

**Every time** you push to main:

1. **Fetch latest first:**
   ```bash
   git fetch origin main
   ```

2. **Check if you're behind:**
   ```bash
   git log origin/main..HEAD
   ```
   - If empty: you're up to date ✓
   - If commits show: other sessions merged work you don't have
     ```bash
     git merge origin/main
     ```

3. **Push with the hook:**
   ```bash
   git push origin main
   ```
   - Hook will verify no reversion is happening
   - Will ask for confirmation before pushing

---

## If the Hook Blocks You

**Scenario:** Pre-push hook says "You're behind origin/main"

**Fix:**
```bash
# 1. Fetch the latest
git fetch origin main

# 2. Merge the remote work
git merge origin/main

# 3. Resolve any conflicts (unlikely but possible)
# Edit files, then:
git add .
git commit -m "Merge origin/main: integrate concurrent session work"

# 4. Try pushing again
git push origin main
```

---

## If Another Session Reverted Your Work

**Detection:**
- You see a commit on main that removes something you added
- Tests pass but functionality is missing
- PR was merged but now it's gone

**Recovery:**
```bash
# 1. Find the commit that reverted your work
git log --oneline origin/main | head -20

# 2. Identify which PR was reverted
# Look for commits with "Revert" or "Remove X" after your PR merged

# 3. Verify it's genuinely lost (not a false alarm)
git show <commit-that-looks-wrong>

# 4. If lost, re-merge the PR or cherry-pick the commit
# If reversion was intentional, update your local branch to match
git fetch origin main
git reset --hard origin/main
```

---

## Parallel Session Protocol

**If you're aware another session is working on this repo:**

1. **Coordinate on branches** — different features, different branches
2. **Don't both push to main** — designate who merges PRs
3. **Fetch before any push** — always check what's already merged
4. **If unavoidable collision** — rebase onto latest main, not merge

**If you're NOT aware:**
- The pre-push hook will catch most conflicts
- But not all (hook can't detect every type of problem)
- Code review + tests are final defense

---

## Why This Matters

Six PRs merged today without increasing revenue. The notary recruitment messages are the only path to first dollars. 

Infrastructure work is necessary but **not sufficient**. If parallel sessions keep reverting each other's work, we lose compounding progress and can't even trust what's merged.

This hook is a forcing function: every main push requires explicit verification that you're not reverting someone else's work.

---

## Testing the Hook

```bash
# Create a test branch
git checkout -b test-hook
echo "test" > test.txt
git add test.txt
git commit -m "test commit"

# Try to push to main (should fail because you're on test-hook)
git push origin main
# Should get: nothing happens (hook only runs on main branch)

# Switch to main and try
git checkout main
git merge test-hook
git push origin main
# Should warn and ask for confirmation
```

---

## Long-Term Fix

The hook prevents the **symptom** (pushing without awareness). The **root cause** is:
- Multiple autonomous sessions
- No coordination mechanism
- Each session's knowledge of main is stale

Permanent solutions would require:
1. **Scheduled coordination window** — only one session pushes to main per time window
2. **Merge locks** — only one session can merge at a time
3. **Session awareness** — sessions know about each other and pause/resume
4. **Deterministic ordering** — enforced sequential merges

For now, the hook + the warning above are the safeguard.

---

## Last Updated

- **2026-08-04 19:39 UTC** — Hook installed after #140/#143 reversion incident
- **Status:** ✅ ACTIVE (all future main pushes protected)
