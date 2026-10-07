"""Two boot-time bugs found in the 2026-10-07 14:27 deploy log, and their fixes.

Run as written:  python3 test_boot_idempotence.py

BOTH HAD THE SAME SHAPE: they did not crash, they just quietly did the
wrong thing on every single boot while looking healthy in the log.
"""
import asyncio
import ast
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="boot-idem-test-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp}/t.db"

FAILED = []


def ok(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILED.append(label)


# ============================================================ 1. STRIPE
print("\n1. STRIPE SETUP IS IDEMPOTENT - a restart adds nothing")
print("   Live evidence: the 14:27 boot created prod_VOjHD2iHAbcx47,")
print("   prod_VOjHlil5zL4T0K and prod_VOjHE4QR2kARke - all brand new,")
print("   on an account that already had the same three tiers.\n")


class _Obj(dict):
    """A Stripe-ish object: attribute access plus .get(), like the real one."""
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)


class _Page(list):
    has_more = False


class FakeStripe:
    """Records every create. The whole point is that the count stays at 3."""
    api_key = "sk_test_fake"

    class error:
        class InvalidRequestError(Exception):
            pass

    def __init__(self):
        self.products, self.prices = [], []
        self.product_creates = self.price_creates = 0
        outer = self

        class Product:
            @staticmethod
            def create(**kw):
                outer.product_creates += 1
                p = _Obj(id=f"prod_{len(outer.products)}",
                         metadata=kw.get("metadata") or {}, active=True,
                         name=kw.get("name"))
                outer.products.append(p)
                return p

            @staticmethod
            def list(**kw):
                return _Page([p for p in outer.products if p.get("active")])

        class Price:
            @staticmethod
            def create(**kw):
                outer.price_creates += 1
                p = _Obj(id=f"price_{len(outer.prices)}", product=kw["product"],
                         unit_amount=kw["unit_amount"], currency=kw["currency"],
                         recurring=kw.get("recurring") or {},
                         metadata=kw.get("metadata") or {}, active=True)
                outer.prices.append(p)
                return p

            @staticmethod
            def list(**kw):
                return _Page([p for p in outer.prices
                              if p["product"] == kw.get("product") and p.get("active")])

        self.Product, self.Price = Product, Price


# stripe is not installed in this sandbox, and this test never calls the real
# one anyway - setup_stripe_products takes its client as a parameter precisely
# so it can be exercised without touching the live account.
import types as _types
if "stripe" not in sys.modules:
    _stub = _types.ModuleType("stripe")
    _stub.api_key = None

    class _E(Exception):
        pass

    _stub.error = _types.SimpleNamespace(InvalidRequestError=_E)
    _stub.Product = _types.SimpleNamespace(create=None, list=None)
    _stub.Price = _types.SimpleNamespace(create=None, list=None)
    sys.modules["stripe"] = _stub

import stripe_subscriptions as SS

fake = FakeStripe()
SS.stripe_price_ids.clear()
ok("first boot succeeds", SS.setup_stripe_products(client=fake) is True)
ok("first boot creates one product per paid tier", fake.product_creates == 3)
ok("first boot creates one price per paid tier", fake.price_creates == 3)
first_ids = dict(SS.stripe_price_ids)

ok("second boot succeeds", SS.setup_stripe_products(client=fake) is True)
ok("SECOND BOOT CREATES NO NEW PRODUCT", fake.product_creates == 3)
ok("SECOND BOOT CREATES NO NEW PRICE", fake.price_creates == 3)
ok("and it resolves to the SAME price ids", dict(SS.stripe_price_ids) == first_ids)

for _ in range(5):
    SS.setup_stripe_products(client=fake)
ok("seven boots in total still leave exactly 3 products",
   len(fake.products) == 3 and fake.product_creates == 3)
ok("seven boots in total still leave exactly 3 prices",
   len(fake.prices) == 3 and fake.price_creates == 3)

# a price that predates the metadata must still be reused, not duplicated
fake2 = FakeStripe()
fake2.products.append(_Obj(id="prod_legacy", metadata={"tier_id": "starter"},
                           active=True, name="Starter Plan"))
fake2.prices.append(_Obj(id="price_legacy", product="prod_legacy",
                         unit_amount=50000, currency="usd",
                         recurring={"interval": "month"}, metadata={}, active=True))
SS.stripe_price_ids.clear()
SS.setup_stripe_products(client=fake2)
ok("a legacy price with no metadata is matched on amount/currency/interval",
   SS.stripe_price_ids.get("starter") == "price_legacy")
ok("and no duplicate was created for it", fake2.price_creates == 2)   # pro + enterprise only

ok("no api key is still a clean False, not an exception",
   SS.setup_stripe_products(client=type("N", (), {"api_key": None})()) is False)


# ================================================== 2. FOREIGN KEY VALIDATION
print("\n2. THE FOREIGN-KEY VALIDATOR CAN ACTUALLY READ THE TABLE LIST")
print("   It failed on its FIRST line every boot - 'AsyncConnection' object")
print("   has no attribute 'sync_conn' - so the missing-table detection and")
print("   repair below it never ran at all.\n")

src = open("main.py").read()
ok("the dead accessor is gone from main.py", "conn.sync_conn" not in src)
ok("the working accessor is used instead",
   "await conn.run_sync(\n                lambda sync_conn: inspect(sync_conn).get_table_names())" in src)
ok("main.py still parses", bool(ast.parse(src)))


async def _reflect():
    """The replacement pattern, exercised against a real async connection."""
    from sqlalchemy import inspect as sa_inspect
    from database import get_engine, Base
    import models  # noqa: F401
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        return {t.lower() for t in await conn.run_sync(
            lambda sync_conn: sa_inspect(sync_conn).get_table_names())}


tables = asyncio.run(_reflect())
ok("run_sync reflection returns a real table list", len(tables) > 10)
ok("and it contains a table the app actually uses",
   "crypto_grid_branches" in tables and "trade_decisions" in tables)


async def _old_way_still_broken():
    from database import get_engine
    async with get_engine().begin() as conn:
        return hasattr(conn, "sync_conn")


ok("the old attribute genuinely does not exist (this was the bug, not a typo)",
   asyncio.run(_old_way_still_broken()) is False)

print("\nNOT PROVED HERE, STATED PLAINLY: validate_foreign_keys() early-returns")
print("on any non-PostgreSQL dialect, so the function body cannot be executed")
print("in this sandbox. What is proved is that the accessor it now uses works")
print("on a real async connection and that the one that always threw is gone.")

# ============================================ 3. THE MODULE LOADS ONCE
print("\n3. main.py IS NOT IMPORTED A SECOND TIME BY ITS OWN LAUNCHER")
print("   The 14:27 log shows the whole router block twice, ~700ms apart:")
print("   `python main.py` runs it as __main__, then uvicorn.run('main:app')")
print("   imports it AGAIN under its real name.\n")

ok("uvicorn is handed the app object, not the import string",
   "uvicorn.run(app, host=" in src)
# Checked against the PARSED code, not the file text - the explanatory
# comment above the fix legitimately quotes the old form, and a substring
# search over the whole file would fail on the comment that documents it.
_str_consts = {n.value for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.Constant) and isinstance(n.value, str)}
ok("the re-importing form appears in no string the code evaluates",
   "main:app" not in _str_consts)
ok("reload is still off, which is what makes the object form equivalent",
   "reload=False" in src)

tree = ast.parse(src)
calls = [n for n in ast.walk(tree)
         if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
         and n.func.attr == "run"
         and getattr(n.func.value, "id", "") == "uvicorn"]
ok("exactly one uvicorn.run call exists", len(calls) == 1)
ok("its first argument is a Name, not a string literal",
   bool(calls) and isinstance(calls[0].args[0], ast.Name))


# ================================== 4. THE BOT WORKER CAN ACTUALLY BE CREATED
print("\n4. THE BOT WORKER ROW CAN BE CREATED AT ALL")
print("   Worker has no `role` column, so role='bot' raised TypeError every")
print("   boot and the handler logged 'will retry later' - forever.\n")

ibw = open("initialize_bot_worker.py").read()
_wkw = set()
for _n in ast.walk(ast.parse(ibw)):
    if (isinstance(_n, ast.Call) and getattr(_n.func, "id", "") == "Worker"):
        _wkw |= {k.arg for k in _n.keywords}
ok("no Worker(...) call passes a `role` kwarg", "role" not in _wkw)
ok("and the call does pass custom_metadata", "custom_metadata" in _wkw)
ok("the intent is kept in a column that exists",
   'custom_metadata={"role": "bot"}' in ibw)
ok("a racing duplicate is handled as success, not as a retryable failure",
   "IntegrityError" in ibw)

from models import Worker as _W
_cols = {c.name for c in _W.__table__.columns}
ok("Worker genuinely has no `role` column (this was the bug, not a typo)",
   "role" not in _cols)
ok("Worker does have custom_metadata", "custom_metadata" in _cols)
try:
    _W(name="x", email="y@z", status="active", custom_metadata={"role": "bot"})
    _constructs = True
except TypeError:
    _constructs = False
ok("the new kwargs actually construct a Worker", _constructs)
try:
    _W(name="x", email="y@z", role="bot", status="active")
    _old_ok = True
except TypeError:
    _old_ok = False
ok("the old kwargs still raise, confirming the diagnosis", _old_ok is False)

print("\n" + ("ALL CHECKS PASSED" if not FAILED else f"{len(FAILED)} FAILED:"))
for f in FAILED:
    print("   - " + f)
sys.exit(1 if FAILED else 0)
