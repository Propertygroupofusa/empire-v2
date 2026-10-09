"""The dry run has one job it must never fail: changing nothing.

Three of these tests are about the module's OWN behaviour and three are
about the thing it is supposed to detect. The read-only tests work on
the AST rather than on the text, because this repository has already
been burned twice by assertions that matched the code's COMMENTS - the
comments in ops_dry_run.py are full of the words "order", "write" and
"429" precisely because they explain why none of those happen, and a
substring search would read that as a violation.
"""

import ast
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "ops_dry_run.py")

with open(SRC, encoding="utf-8") as _fh:
    SOURCE = _fh.read()
TREE = ast.parse(SOURCE)

# Anything that could reach the network or a broker.
FORBIDDEN_IMPORTS = {"aiohttp", "requests", "httpx", "urllib", "urllib3",
                     "socket", "http", "websockets", "sqlalchemy", "asyncpg",
                     "psycopg2", "ccxt", "alpaca", "coinbase"}
# Attribute calls that write, anywhere, on anything.
FORBIDDEN_ATTR_CALLS = {"post", "put", "patch", "delete", "urlopen",
                        "commit", "flush", "execute", "executemany",
                        "setdefault", "clear", "popitem", "writerow",
                        "submit_order", "close_position", "cancel_order"}
# Functions that reach the venue or mutate shared state if called.
FORBIDDEN_NAME_CALLS = {"census", "census_cached", "get_usd_balance",
                        "get_real_free_cash_usd", "fetch_balances",
                        "fetch_accounts", "place_order"}


def _calls(tree):
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


class TheDryRunChangesNothing(unittest.TestCase):

    def test_it_imports_nothing_that_can_reach_a_venue_or_a_database(self):
        imported = set()
        for node in ast.walk(TREE):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        bad = imported & FORBIDDEN_IMPORTS
        self.assertEqual(set(), bad,
                         f"ops_dry_run imports {sorted(bad)} - a dry run that "
                         f"can open a socket is one edit away from not being "
                         f"one")

    def test_it_calls_no_write_verb_and_no_venue_function(self):
        bad = []
        for call in _calls(TREE):
            f = call.func
            if isinstance(f, ast.Attribute) and f.attr in FORBIDDEN_ATTR_CALLS:
                bad.append(f.attr)
            if isinstance(f, ast.Name) and f.id in FORBIDDEN_NAME_CALLS:
                bad.append(f.id)
        self.assertEqual([], sorted(set(bad)),
                         "a write verb or venue call is present. Note these "
                         "are AST Call nodes, so a mention in a comment or a "
                         "string literal cannot be what tripped this.")

    def test_it_never_assigns_to_the_census_cache(self):
        """Reading _CENSUS_CACHE is fine. Binding it, or any subscript of
        it, is not - that is the live cache the trading loop shares."""
        offences = []
        for node in ast.walk(TREE):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for t in targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name) and sub.id == "_CENSUS_CACHE":
                        offences.append(ast.dump(node)[:80])
        self.assertEqual([], offences)

    def test_apply_tracked_is_handed_a_copy_never_the_cached_dict(self):
        """apply_tracked mutates its argument. The endpoint passes
        dict(cached); so must this, or a dry run corrupts the cache it
        was only supposed to look at."""
        found = 0
        for call in _calls(TREE):
            f = call.func
            is_apply = ((isinstance(f, ast.Attribute) and f.attr == "apply_tracked")
                        or (isinstance(f, ast.Name) and f.id == "apply_tracked"))
            if not is_apply or not call.args:
                continue
            first = call.args[0]
            found += 1
            self.assertTrue(
                isinstance(first, ast.Call) and isinstance(first.func, ast.Name)
                and first.func.id == "dict",
                "apply_tracked is called on something that is not dict(...) - "
                "it mutates what it is given")
        self.assertGreaterEqual(found, 1, "the arithmetic check disappeared")

    def test_the_payload_reports_zero_venue_requests_and_says_so(self):
        import ops_dry_run
        out = ops_dry_run.dry_run()
        self.assertTrue(out["executes_nothing"])
        self.assertEqual(0, out["venue_requests_made"])

    def test_no_sensitive_value_can_reach_the_payload(self):
        """Set every sensitive name to a sentinel and prove none of the
        sentinels appear anywhere in the output, at any depth."""
        import json
        import ops_dry_run
        saved = {}
        sentinels = {}
        try:
            for i, name in enumerate(sorted(ops_dry_run.SENSITIVE_ENV)):
                saved[name] = os.environ.get(name)
                sentinels[name] = f"SENTINEL-{i}-DO-NOT-LEAK"
                os.environ[name] = sentinels[name]
            blob = json.dumps(ops_dry_run.dry_run()) + ops_dry_run.report()
            for name, s in sentinels.items():
                self.assertNotIn(s, blob, f"{name}'s VALUE reached the payload")
                # the NAME may appear - absence of a credential is
                # reportable; its contents are not.
        finally:
            for name, old in saved.items():
                if old is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old


class ItDetectsTheCodeItIsCheckingFor(unittest.TestCase):
    """The only thing an in-process dry run can do that an HTTP check
    cannot: say whether the LOADED code is the new code, with no request.
    These two tests are the whole value of the module, so they assert
    both directions - pass on the fix, fail on the rollback target."""

    def _fingerprint(self, census_src, grid_src):
        """Run the code_is_live logic against supplied sources."""
        import ops_dry_run
        c_fn = ops_dry_run._fn(ast.parse(census_src), "census_cached")
        ok_a, _ = ops_dry_run._cache_store_is_caller_agnostic(c_fn)
        g_fn = ops_dry_run._fn(ast.parse(grid_src), "get_real_free_cash_usd")
        names = [a.arg for a in g_fn.args.args] + \
                [a.arg for a in g_fn.args.kwonlyargs]
        return ok_a, "usd_balance" in names

    def test_it_passes_on_the_code_in_this_working_tree(self):
        with open(os.path.join(HERE, "account_census.py"), encoding="utf-8") as fh:
            census = fh.read()
        with open(os.path.join(HERE, "crypto_grid_bot.py"), encoding="utf-8") as fh:
            grid = fh.read()
        ok_a, ok_b = self._fingerprint(census, grid)
        self.assertTrue(ok_a, "the cache-store fingerprint stopped matching")
        self.assertTrue(ok_b, "the usd_balance seam stopped matching")

    def test_it_fails_when_the_cache_stores_a_callers_comparison(self):
        """The pre-fix shape, reduced to its defect: the cache keeps the
        dict that already had one caller's tracked_usd applied."""
        before = (
            "def census_cached(session, tracked_usd=None):\n"
            "    fresh = {}\n"
            "    out = apply_tracked(dict(fresh), tracked_usd)\n"
            "    _CENSUS_CACHE.update(census=out, at=0)\n"
            "    return out\n")
        grid_ok = "def get_real_free_cash_usd(usd_balance=None):\n    return 0\n"
        ok_a, ok_b = self._fingerprint(before, grid_ok)
        self.assertFalse(ok_a)
        self.assertTrue(ok_b, "only the census half should have failed")

    def test_it_fails_when_apply_tracked_is_stored_with_a_caller_value(self):
        sneaky = (
            "def census_cached(session, tracked_usd=None):\n"
            "    fresh = {}\n"
            "    _CENSUS_CACHE.update(census=apply_tracked(dict(fresh), tracked_usd), at=0)\n"
            "    return fresh\n")
        grid_ok = "def get_real_free_cash_usd(usd_balance=None):\n    return 0\n"
        ok_a, _ = self._fingerprint(sneaky, grid_ok)
        self.assertFalse(ok_a, "apply_tracked with a caller's value is still "
                               "a poisoned cache")

    def test_it_fails_when_the_grid_cannot_be_handed_a_balance(self):
        census_ok = (
            "def census_cached(session, tracked_usd=None):\n"
            "    fresh = {}\n"
            "    _CENSUS_CACHE.update(census=apply_tracked(dict(fresh), None), at=0)\n"
            "    return apply_tracked(dict(fresh), tracked_usd)\n")
        before = "def get_real_free_cash_usd():\n    return 0\n"
        ok_a, ok_b = self._fingerprint(census_ok, before)
        self.assertTrue(ok_a)
        self.assertFalse(ok_b)


class UnknownIsNotAPass(unittest.TestCase):

    def test_an_unsettled_check_reads_as_unknown(self):
        import ops_dry_run
        self.assertEqual("UNKNOWN", ops_dry_run.Check("x").verdict)
        self.assertEqual("PASS", ops_dry_run.Check("x", True).verdict)
        self.assertEqual("FAIL", ops_dry_run.Check("x", False).verdict)

    def test_reachability_is_unknown_on_purpose_and_never_pass(self):
        import ops_dry_run
        c = ops_dry_run._check_dependencies()
        self.assertIsNone(c.ok)
        self.assertIn("not proven", c.detail)

    def test_a_failure_outranks_an_unknown_in_the_verdict(self):
        """A real failure must not be softened to READY_WITH_UNKNOWNS
        just because something else went unmeasured."""
        import ops_dry_run
        orig = ops_dry_run.CHECKS
        try:
            ops_dry_run.CHECKS = (
                lambda: ops_dry_run.Check("a", True),
                lambda: ops_dry_run.Check("b", None),
                lambda: ops_dry_run.Check("c", False),
            )
            self.assertEqual("NOT_READY", ops_dry_run.dry_run()["verdict"])
            ops_dry_run.CHECKS = (lambda: ops_dry_run.Check("a", True),
                                  lambda: ops_dry_run.Check("b", None))
            self.assertEqual("READY_WITH_UNKNOWNS",
                             ops_dry_run.dry_run()["verdict"])
            ops_dry_run.CHECKS = (lambda: ops_dry_run.Check("a", True),)
            self.assertEqual("READY", ops_dry_run.dry_run()["verdict"])
        finally:
            ops_dry_run.CHECKS = orig

    def test_a_check_that_raises_becomes_a_failure_not_a_500(self):
        import ops_dry_run
        orig = ops_dry_run.CHECKS

        def boom():
            raise RuntimeError("the venue exploded")
        try:
            ops_dry_run.CHECKS = (boom,)
            out = ops_dry_run.dry_run()
            self.assertEqual("NOT_READY", out["verdict"])
            self.assertIn("the venue exploded", out["checks"][0]["detail"])
        finally:
            ops_dry_run.CHECKS = orig


class TheUsdRowIsTakenFromTheUsdRow(unittest.TestCase):

    def _with_cache(self, census):
        import sys
        import types
        import ops_dry_run
        mod = types.ModuleType("account_census")
        mod._CENSUS_CACHE = {"census": census, "at": 1.0}
        mod.CENSUS_TTL_SECONDS = 45.0
        saved = sys.modules.get("account_census")
        sys.modules["account_census"] = mod
        try:
            return ops_dry_run._check_usd_row(), mod._CENSUS_CACHE["census"]
        finally:
            if saved is None:
                sys.modules.pop("account_census", None)
            else:
                sys.modules["account_census"] = saved

    def test_it_reports_the_usd_row_and_cash_usd_separately(self):
        """Tonight's live shape: cash_usd 543.40 against a 242.13 USD
        wallet, because the account is held in USDC."""
        census = {"cash_usd": 543.40, "total_usd": 6644.40, "holdings": [
            {"asset": "USD", "available_units": 242.1332093343912},
            {"asset": "USDC", "available_units": 301.2599658725513},
        ]}
        c, _ = self._with_cache(census)
        self.assertAlmostEqual(242.13, c.facts["usd_available_units"], places=2)
        self.assertAlmostEqual(543.40, c.facts["cash_usd"], places=2)
        self.assertAlmostEqual(301.27, c.facts["stable_minus_usd"], places=2)

    def test_a_missing_usd_row_is_zero_not_none(self):
        """This account held no USD row at all for stretches of
        2026-10-08. None would propagate into arithmetic as a crash or,
        worse, as a float."""
        census = {"cash_usd": 301.26, "total_usd": 6000.0, "holdings": [
            {"asset": "USDC", "available_units": 301.26}]}
        c, _ = self._with_cache(census)
        self.assertEqual(0.0, c.facts["usd_available_units"])

    def test_looking_at_the_cache_does_not_change_the_cache(self):
        census = {"cash_usd": 1.0, "total_usd": 2.0, "tracked_usd": 99.0,
                  "holdings": [{"asset": "USD", "available_units": 1.0}]}
        before = dict(census)
        _, after = self._with_cache(census)
        self.assertEqual(before, after)
        self.assertEqual(99.0, after["tracked_usd"],
                         "the dry run overwrote the cached comparison")


if __name__ == "__main__":
    unittest.main(verbosity=2)
