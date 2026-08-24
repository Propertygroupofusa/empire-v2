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
import time
from datetime import datetime, timezone

from database import AsyncSessionLocal

log = logging.getLogger("status_snapshot")

SNAPSHOT_INTERVAL_SECONDS = int(os.getenv("STATUS_SNAPSHOT_INTERVAL_SECONDS", str(30 * 60)))
GITHUB_TOKEN = os.getenv("STATUS_SNAPSHOT_GITHUB_TOKEN", "")
REPO_SLUG = os.getenv("STATUS_SNAPSHOT_REPO_SLUG", "Propertygroupofusa/empire-v2")
SNAPSHOT_BRANCH = os.getenv("STATUS_SNAPSHOT_BRANCH", "status-snapshots")
SNAPSHOT_FILENAME = "STATUS.md"
REPO_DIR = os.path.dirname(os.path.abspath(__file__))


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

    lines = [
        "## 🌳 Crypto Family Tree",
        "",
        f"- **Total Profit (realized + unrealized):** {_fmt_usd(total_profit)}",
        f"- **Total Allocated:** {_fmt_usd(status.get('total_allocated_usd'))}",
        f"- **Locked Profit:** {_fmt_usd(status.get('locked_usd'))}",
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

    lines = [
        "## 📈 Alpaca (Stocks/Futures)",
        "",
        f"- **Total Profit:** {_fmt_usd(total_profit)}",
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


async def build_snapshot_markdown() -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    crypto_section = await _build_crypto_section()
    alpaca_section = await _build_alpaca_section()
    return (
        f"# Empire v2 — Real Status Snapshot\n\n"
        f"_Generated automatically at {generated_at}. Read-only real account data - "
        f"this file is written by the running app on a timer and pushed to the "
        f"`{SNAPSHOT_BRANCH}` branch (never `main`). Numbers reflect the live database "
        f"and, where noted, live Coinbase/Alpaca prices at generation time._\n\n"
        f"{crypto_section}\n{alpaca_section}"
    )


def _run_git(args, timeout=30):
    return subprocess.run(
        ["git"] + args, cwd=REPO_DIR, capture_output=True, text=True, timeout=timeout
    )


def push_snapshot(content: str) -> bool:
    """Writes STATUS.md and force-pushes a single fresh commit to
    SNAPSHOT_BRANCH. Force-push is deliberate and safe here: this branch
    only ever exists to hold the latest snapshot, never accumulated
    history, and nothing else should ever push to it. Never touches
    main - this function has no code path that can reach main."""
    if not GITHUB_TOKEN:
        log.info("[STATUS-SNAPSHOT] STATUS_SNAPSHOT_GITHUB_TOKEN not set - skipping push (snapshot generation still ran)")
        return False

    if not os.path.isdir(os.path.join(REPO_DIR, ".git")):
        log.warning("[STATUS-SNAPSHOT] no .git directory found in this deployment - cannot push")
        return False

    snapshot_path = os.path.join(REPO_DIR, SNAPSHOT_FILENAME)
    try:
        with open(snapshot_path, "w") as f:
            f.write(content)

        _run_git(["config", "--local", "user.email", "status-snapshot@empire-v2.local"])
        _run_git(["config", "--local", "user.name", "Empire Status Snapshot"])

        add = _run_git(["add", "-f", SNAPSHOT_FILENAME])
        if add.returncode != 0:
            log.warning(f"[STATUS-SNAPSHOT] git add failed: {add.stderr}")
            return False

        commit = _run_git(["commit", "-m", f"Status snapshot {datetime.now(timezone.utc).isoformat()}"])
        if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
            log.warning(f"[STATUS-SNAPSHOT] git commit failed: {commit.stderr}")
            return False

        remote_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{REPO_SLUG}.git"
        push = _run_git(["push", "--force", remote_url, f"HEAD:refs/heads/{SNAPSHOT_BRANCH}"], timeout=60)
        if push.returncode != 0:
            # Never let the real push URL (with the token embedded) reach
            # the logs - only the generic git stderr, which git itself
            # does not include the URL in for a plain push failure.
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
