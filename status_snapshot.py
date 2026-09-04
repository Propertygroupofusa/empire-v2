"""Periodic real-status snapshot, committed to a dedicated git branch
(never main) so it can be read by tools with no live network access to
this app, Coinbase, or Alpaca, but WITH normal git access to this
repository - specifically so a Claude Code session working on this
codebase can see real recent trading numbers without needing live API
credentials or network egress into this deployment.

Read-only by design: this module only ever reads real DB/account state
and writes a plain-text file. It has no path to placing an order,
changing a position, or touching anything trading-related - the only
"write" it ever performs is a git commit of a status report.

Requires STATUS_SNAPSHOT_GITHUB_TOKEN (a token scoped to just this repo,
Contents: Read and write) as an env var. If unset, run() logs once and
returns - every other part of the app is completely unaffected.
"""
import asyncio
import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone

from sqlalchemy import select

from database import AsyncSessionLocal
from models import TradingBotState

log = logging.getLogger("status_snapshot")

SNAPSHOT_INTERVAL_SECONDS = int(os.getenv("STATUS_SNAPSHOT_INTERVAL_SECONDS", str(30 * 60)))
GITHUB_TOKEN = os.getenv("STATUS_SNAPSHOT_GITHUB_TOKEN", "")
REPO_SLUG = os.getenv("STATUS_SNAPSHOT_REPO_SLUG", "Propertygroupofusa/empire-v2")
SNAPSHOT_BRANCH = os.getenv("STATUS_SNAPSHOT_BRANCH", "status-snapshots")
SNAPSHOT_FILENAME = "STATUS.md"


def _fmt_usd(n) -> str:
    if n is None:
        return "—"
    sign = "-" if n < 0 else ""
    return f"{sign}${abs(n):,.2f}"


async def _build_crypto_section() -> str:
    try:
        import routers.trading_dashboard as td
    except Exception as e:
        return f"## 🌳 Crypto Family Tree\n\n_Could not load: {e}_\n"

    async with AsyncSessionLocal() as db:
        try:
            status = await td.get_family_tree_status(db=db)
        except Exception as e:
            return f"## 🌳 Crypto Family Tree\n\n_Could not fetch: {e}_\n"

    async with AsyncSessionLocal() as db:
        try:
            coin_history = await td.get_coin_trade_history(db=db)
        except Exception as e:
            coin_history = {"coins": []}

    branches = status.get("branches", [])
    realized = sum(c.get("total_pnl", 0.0) for c in coin_history.get("coins", []))
    # Real per-branch unrealized P&L, matching the dashboard's own JS calc
    # (qty * (current_price - entry_price)) - only counts a branch that's
    # actually holding a position with a real current price available.
    unrealized = 0.0
    for b in branches:
        pos = b.get("position")
        if pos and pos.get("current_price") is not None and pos.get("entry_price") is not None and pos.get("qty") is not None:
            unrealized += pos["qty"] * (pos["current_price"] - pos["entry_price"])

    total_profit = realized + unrealized

    # Real "is this thing even ABLE to trade right now" diagnostics - per
    # the account owner's own direct question ("why isn't it trading, I
    # need to recover that money") this section had no way to answer
    # before: a $0.00 Total Allocated alone doesn't say WHY - it could be
    # unfunded, retired (crypto_passive_mode), or tree-wide paused on a
    # real negative rolling expectancy, three genuinely different real
    # reasons with three different real fixes. All three real values
    # already exist on `status` (the same dict the dashboard itself
    # reads) - no new fetch needed here.
    passive_mode = status.get("crypto_passive_mode")
    spendable = status.get("spendable_for_spawn")
    real_usd = status.get("real_usd_balance")
    real_usdc = status.get("real_usdc_balance")
    rolling = status.get("rolling_expectancy") or {}

    lines = [
        "## 🌳 Crypto Family Tree",
        "",
        f"- **Total Profit (realized + unrealized):** {_fmt_usd(total_profit)}",
        "- **Real starting capital:** not tracked anywhere in this app - unlike Alpaca's bot buckets (see below), no `starting_capital` snapshot was ever recorded for the crypto side (family tree or Grid Bot). The real original deposit amount can only be found in Coinbase's own account/deposit history directly.",
        f"- **Total Allocated:** {_fmt_usd(status.get('total_allocated_usd'))}",
        f"- **Locked Profit:** {_fmt_usd(status.get('locked_usd'))}",
        f"- **Retired (buy-and-hold BTC passive mode):** {'YES - no new entries, existing positions unmanaged' if passive_mode else 'No - actively trading'}",
        f"- **Real free cash available to fund a branch:** {_fmt_usd(spendable)}",
        f"- **Real Coinbase USD / USDC balance:** {_fmt_usd(real_usd)} / {_fmt_usd(real_usdc)}",
        # The exact figure the combined $1M tracker's crypto side reads. Printed
        # here so a session with no live dashboard access can verify the real
        # number DIRECTLY instead of re-deriving it from wallet balance + branch
        # holdings and hoping the arithmetic matches - the precise gap that made
        # the "-53% crash that never happened" bug hard to confirm fixed. A real
        # "—" means an unpriceable holding or a failed balance fetch this poll:
        # the tracker honestly reports the crypto side unavailable rather than a
        # partial total, and skips logging a snapshot (never a fabricated 0).
        f"- **Real crypto net worth (what the combined $1M tracker uses):** "
        f"{_fmt_usd(status.get('real_crypto_net_worth_usd'))} "
        "= real USD wallet + market value of every coin the tree and Grid Bot actually hold",
        f"- **Tree-wide entries paused (negative rolling expectancy):** "
        + (f"YES - last {rolling.get('num_trades')} real trades averaged {_fmt_usd(rolling.get('expectancy'))}/trade (real total {_fmt_usd(rolling.get('total_pnl'))})" if rolling.get("negative") else "No"),
        f"- **Branches:** {len(branches)}",
        "",
        "| Branch | Coin | Balance | Position | Unrealized P&L | Last Order Error |",
        "|---|---|---|---|---|---|",
    ]
    for b in branches:
        pos = b.get("position")
        if pos and pos.get("current_price") is not None and pos.get("entry_price") is not None:
            pnl = pos["qty"] * (pos["current_price"] - pos["entry_price"])
            pos_desc = f"{pos['qty']:.4f} @ {_fmt_usd(pos['entry_price'])}"
            pnl_desc = _fmt_usd(pnl)
        else:
            pos_desc = "flat"
            pnl_desc = "—"
        err = b.get("last_order_error") or "—"
        lines.append(
            f"| {b['bot_name']} | {b['product_id']} | {_fmt_usd(b.get('allocated_usd'))} "
            f"| {pos_desc} | {pnl_desc} | {err} |"
        )

    lines.append("")
    lines.append("### Per-coin real trade history")
    lines.append("")
    lines.append("| Coin | Trades | Win Rate | Total P&L |")
    lines.append("|---|---|---|---|")
    for c in coin_history.get("coins", []):
        lines.append(
            f"| {c['product_id']} | {c['trade_count']} | {c.get('win_rate', 0):.1f}% | {_fmt_usd(c.get('total_pnl'))} |"
        )
    if not coin_history.get("coins"):
        lines.append("| _(no completed trades recorded yet)_ | | | |")

    return "\n".join(lines) + "\n"


async def _build_alpaca_section() -> str:
    try:
        import routers.trading_dashboard as td
    except Exception as e:
        return f"## 📈 Alpaca (Stocks/Futures)\n\n_Could not load: {e}_\n"

    async with AsyncSessionLocal() as db:
        try:
            data = await td.get_alpaca_overview(db=db)
        except Exception as e:
            return f"## 📈 Alpaca (Stocks/Futures)\n\n_Could not fetch: {e}_\n"

    bots = data.get("bots", [])
    total_profit = sum(b.get("pl", 0.0) for b in bots)

    # Real starting capital - per the account owner's direct question
    # ("how much did I actually start off with"). A pure DB value (never
    # needs the real live Alpaca fetch get_alpaca_overview() above already
    # made), so queried directly here rather than added to that dict.
    # Real, honest caveat baked into the label itself: TradingBotState.
    # starting_capital is documented as "a never-updated snapshot of what
    # the bucket started at" - but a bucket created BEFORE this field
    # existed had it silently backfilled to whatever its real base_capital
    # happened to be at that later moment (see _get_or_init_bots's own
    # backfill), not necessarily the true original day-one deposit. This
    # is the most accurate real number this app has ever recorded, not a
    # guaranteed exact original deposit.
    alpaca_starting_capital = None
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name.like(f"{td.BOT_PREFIX}%")))
            bot_rows = list(result.scalars().all())
        if bot_rows:
            alpaca_starting_capital = round(sum(b.starting_capital if b.starting_capital is not None else b.base_capital for b in bot_rows), 2)
    except Exception:
        pass

    lines = [
        "## 📈 Alpaca (Stocks/Futures)",
        "",
        f"- **Total Profit:** {_fmt_usd(total_profit)}",
        f"- **Real starting capital (earliest recorded, may not be the true original deposit - see note below):** {_fmt_usd(alpaca_starting_capital)}",
        f"- **Equity:** {_fmt_usd(data.get('equity'))}",
        f"- **Cash:** {_fmt_usd(data.get('cash'))}",
        f"- **Session P&L:** {_fmt_usd(data.get('session_pl'))}",
        f"- **Locked Profit:** {_fmt_usd(data.get('locked_usd'))}",
        "",
        "| Bucket | Capital | P&L |",
        "|---|---|---|",
    ]
    for b in bots:
        lines.append(f"| {b['name']} | {_fmt_usd(b.get('capital'))} | {_fmt_usd(b.get('pl'))} |")

    positions = data.get("positions", [])
    lines.append("")
    lines.append("### Open positions")
    lines.append("")
    if positions:
        lines.append("| Symbol | Side | Qty | Entry | Current | Unrealized P&L |")
        lines.append("|---|---|---|---|---|---|")
        for p in positions:
            entry = p.get("avg_entry_price")
            current = p.get("current_price")
            qty = p.get("qty")
            pnl = (current - entry) * qty if entry is not None and current is not None and qty is not None else None
            lines.append(
                f"| {p.get('symbol')} | {p.get('side')} | {qty} | {_fmt_usd(entry)} | {_fmt_usd(current)} | {_fmt_usd(pnl)} |"
            )
    else:
        lines.append("_(no open positions)_")

    return "\n".join(lines) + "\n"


async def _build_grid_section() -> str:
    """Real Grid Bot status - added after the account owner asked to
    "check the dashboard" for the Grid Bot features (the drawdown
    breaker, dynamic spacing, the $20 Quick Buy button) three separate
    times, and this session had no way to actually answer that: this
    file existed before Grid Bot did and was never extended to cover it,
    so the one real channel available with no live network access
    (this git snapshot) was blind to the one thing being asked about
    most. Calls crypto_grid_bot.get_grid_status() directly - the exact
    same real function the dashboard's own /grid-status endpoint calls,
    so this can never show a different reality than the live page
    does."""
    try:
        import crypto_grid_bot as grid
    except Exception as e:
        return f"## 🔲 Grid Bot\n\n_Could not load: {e}_\n"

    try:
        status = await grid.get_grid_status()
    except Exception as e:
        return f"## 🔲 Grid Bot\n\n_Could not fetch: {e}_\n"

    branches = status.get("branches", [])

    # Real, closed-trade P&L - per the account owner's own direct
    # question ("is Grid Bot in the negative") which this section
    # couldn't answer at all before: every other section here (the
    # family tree, Alpaca) already surfaces a real total-profit figure,
    # but Grid Bot's own realized P&L across every real completed
    # buy-low/sell-high slice was never summed anywhere in this
    # snapshot. Reuses get_grid_trade_history() - the exact same real
    # function the dashboard's own trade-history panel calls - rather
    # than a second, separately-computed number. Real, honest caveat:
    # summed by CURRENT bot_name, and a branch's bot_name is reassigned
    # on every real coin rotation (see the rotation-cooldown fix above),
    # so this is a real total across every closed slice ever, not a
    # stable per-branch history.
    total_realized_pnl = None
    try:
        trade_history = await grid.get_grid_trade_history()
        total_realized_pnl = round(sum(b["total_pnl"] for b in trade_history.get("branches", [])), 2)
    except Exception:
        pass

    lines = [
        "## 🔲 Grid Bot",
        "",
        f"- **Mode:** {'ON' if status.get('mode_active') else 'OFF'}",
        f"- **Dynamic spacing:** {'ON' if status.get('dynamic_spacing_active') else 'OFF (fixed 1%)'}",
        f"- **Drawdown breaker:** {status.get('drawdown_breaker_pct', 0) * 100:.0f}% off peak",
        f"- **Total Allocated:** {_fmt_usd(status.get('total_allocated_usd'))}",
        f"- **Real Free Cash:** {_fmt_usd(status.get('real_free_cash_usd'))}",
        f"- **Real Realized P&L (all closed slices ever):** {_fmt_usd(total_realized_pnl)}",
        f"- **Branches:** {len(branches)}",
        "",
        "| Branch | Coin | Allocated | Spacing | Levels | Open Slices | Current Price | Peak/Drawdown | Locked |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for b in branches:
        dd = b.get("drawdown_pct")
        dd_desc = f"{_fmt_usd(b.get('peak_equity'))} ({dd * 100:.0f}% down)" if dd is not None else "—"
        if b.get("drawdown_breached"):
            dd_desc = f"🛑 {dd_desc}"
        lines.append(
            f"| {b['bot_name']} | {b['product_id']} | {_fmt_usd(b.get('allocated_usd'))} "
            f"| {b.get('grid_pct', 0) * 100:.2f}% | {b.get('num_levels')} | {b.get('open_slices')} "
            f"| {_fmt_usd(b.get('current_price'))} | {dd_desc} | {'🔒' if b.get('locked') else ''} |"
        )
    if not branches:
        lines.append("| _(no grid branches yet)_ | | | | | | | | |")

    return "\n".join(lines) + "\n"


async def _build_activity_section() -> str:
    """Real, recent Live Activity feed excerpt - added so a session with
    no live network access can see WHAT actually happened recently (a
    move-cash, a reallocation, a spawn, a real sell) instead of only the
    resulting balances, which alone can't explain a real change like a
    branch suddenly draining to $0. Reads the exact same real
    CryptoActivityEvent table the dashboard's own Live Activity panel
    does - shared between the family tree AND Grid Bot (see
    crypto_grid_bot._log_activity_safe), so this one section covers both."""
    try:
        import crypto_family_tree_bot as tree
    except Exception as e:
        return f"## 📡 Recent Activity\n\n_Could not load: {e}_\n"

    try:
        events = await tree.get_activity_feed(limit=40)
    except Exception as e:
        return f"## 📡 Recent Activity\n\n_Could not fetch: {e}_\n"

    lines = ["## 📡 Recent Activity (most recent first)", ""]
    if not events:
        lines.append("_(no activity recorded yet)_")
    else:
        lines.append("| When (UTC) | Branch | Type | What happened |")
        lines.append("|---|---|---|---|")
        for e in events:
            when = (e.get("created_at") or "—").replace("T", " ")[:19]
            msg = (e.get("message") or "").replace("|", "/")
            lines.append(f"| {when} | {e.get('bot_name') or '—'} | {e.get('event_type') or '—'} | {msg} |")

    return "\n".join(lines) + "\n"


async def build_snapshot_markdown() -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    crypto_section = await _build_crypto_section()
    grid_section = await _build_grid_section()
    alpaca_section = await _build_alpaca_section()
    activity_section = await _build_activity_section()
    return (
        f"# Empire v2 — Real Status Snapshot\n\n"
        f"_Generated automatically at {generated_at}. Read-only real account data - "
        f"this file is written by the running app on a timer and pushed to the "
        f"`{SNAPSHOT_BRANCH}` branch (never `main`). Numbers reflect the live database "
        f"and, where noted, live Coinbase/Alpaca prices at generation time._\n\n"
        f"{crypto_section}\n{grid_section}\n{alpaca_section}\n{activity_section}"
    )


def _run_git(args, cwd, timeout=30):
    return subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )


def push_snapshot(content: str) -> bool:
    """Writes STATUS.md and force-pushes a single fresh commit to
    SNAPSHOT_BRANCH, from a throwaway git repo created just for this push
    - NOT from the running app's own directory/git state.

    Real bug found live: Railway's deployed container has no .git
    directory at all ("no .git directory found in this deployment -
    cannot push") - Railway's build process hands the running app its
    source files, not a full git checkout with history. The original
    version of this function assumed the app's own directory was a real
    git working tree, which is true in local dev but not in this actual
    deployment.

    Since this branch only ever holds a single force-pushed snapshot
    commit with no shared history requirement (see the module docstring -
    "never accumulated history"), there was never a real need to reuse
    the app directory's own git history in the first place: a fresh
    `git init` in a throwaway temp directory produces an equally valid
    commit to force-push, and is actually safer than the original
    approach - it never writes into or runs git commands against the
    real running app's own source directory, whether or not a .git
    happens to exist there."""
    if not GITHUB_TOKEN:
        log.info("[STATUS-SNAPSHOT] STATUS_SNAPSHOT_GITHUB_TOKEN not set - skipping push (snapshot generation still ran)")
        return False

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            init = _run_git(["init", "-q"], cwd=tmp_dir)
            if init.returncode != 0:
                log.warning(f"[STATUS-SNAPSHOT] git init failed: {init.stderr}")
                return False

            _run_git(["config", "--local", "user.email", "status-snapshot@empire-v2.local"], cwd=tmp_dir)
            _run_git(["config", "--local", "user.name", "Empire Status Snapshot"], cwd=tmp_dir)

            snapshot_path = os.path.join(tmp_dir, SNAPSHOT_FILENAME)
            with open(snapshot_path, "w") as f:
                f.write(content)

            add = _run_git(["add", SNAPSHOT_FILENAME], cwd=tmp_dir)
            if add.returncode != 0:
                log.warning(f"[STATUS-SNAPSHOT] git add failed: {add.stderr}")
                return False

            commit = _run_git(["commit", "-m", f"Status snapshot {datetime.now(timezone.utc).isoformat()}"], cwd=tmp_dir)
            if commit.returncode != 0:
                log.warning(f"[STATUS-SNAPSHOT] git commit failed: {commit.stderr}")
                return False

            remote_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{REPO_SLUG}.git"
            push = _run_git(["push", "--force", remote_url, f"HEAD:refs/heads/{SNAPSHOT_BRANCH}"], cwd=tmp_dir, timeout=60)
            if push.returncode != 0:
                # Never let the real push URL (with the token embedded)
                # reach the logs - only the generic git stderr, which git
                # itself does not include the URL in for a plain push
                # failure.
                log.warning(f"[STATUS-SNAPSHOT] git push failed: {push.stderr}")
                return False

            log.info(f"[STATUS-SNAPSHOT] pushed real status snapshot to {SNAPSHOT_BRANCH}")
            return True
    except Exception as e:
        log.warning(f"[STATUS-SNAPSHOT] snapshot push failed: {e}")
        return False


async def generate_and_push():
    content = await build_snapshot_markdown()
    push_snapshot(content)


def run():
    """Background daemon thread entry point, started the same way every
    other bot module's run() is started from main.py's lifespan."""
    if not GITHUB_TOKEN:
        log.info(
            "[STATUS-SNAPSHOT] STATUS_SNAPSHOT_GITHUB_TOKEN not configured - "
            "status snapshots are disabled. Set it (a GitHub token scoped to "
            "just this repo, Contents: Read and write) to enable."
        )
        return

    log.info(f"[STATUS-SNAPSHOT] starting - snapshot every {SNAPSHOT_INTERVAL_SECONDS}s, branch '{SNAPSHOT_BRANCH}'")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        try:
            loop.run_until_complete(generate_and_push())
        except Exception as e:
            log.warning(f"[STATUS-SNAPSHOT] cycle failed: {e}")
        time.sleep(SNAPSHOT_INTERVAL_SECONDS)
