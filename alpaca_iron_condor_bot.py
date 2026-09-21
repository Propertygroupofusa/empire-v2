#!/usr/bin/env python3
"""Automated, paper-only iron condor trading through Alpaca's REST APIs."""

import argparse
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger(__name__)

OCC_PATTERN = re.compile(r"^(.+?)(\d{6})([CP])(\d{8})$")
FINAL_ORDER_STATUSES = {"canceled", "expired", "rejected", "replaced", "suspended"}


def eastern_now() -> datetime:
    try:
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception as exc:
        raise RuntimeError(
            "Eastern timezone data is unavailable; install project requirements"
        ) from exc

ENTRY_RULES = {
    "underlyings": ["SPY", "QQQ", "IWM"],
    "dte_min": 30,
    "dte_max": 45,
    "short_delta_min": 0.20,
    "short_delta_max": 0.30,
    "spread_width": Decimal("5.00"),
    "target_credit_min": Decimal("0.25"),
    "target_credit_max": Decimal("0.50"),
    "limit_order_discount": Decimal("0.05"),
    "position_size": 1,
    "max_risk_pct": Decimal("0.02"),
}

EXIT_RULES = {
    "profit_target_pct": Decimal("0.50"),
    "stop_loss_pct": Decimal("1.00"),
    "days_to_expiry_exit": 5,
}

MARKET_HOURS = {
    "entry_window_start": "10:00",
    "entry_window_end": "12:00",
    "exit_window_start": "14:00",
    "exit_window_end": "14:30",
}


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class OptionQuote:
    symbol: str
    underlying: str
    expiration: date
    option_type: str
    strike: Decimal
    delta: Decimal
    bid: Decimal
    ask: Decimal


@dataclass(frozen=True)
class IronCondorCandidate:
    underlying: str
    expiration: date
    short_call: OptionQuote
    long_call: OptionQuote
    short_put: OptionQuote
    long_put: OptionQuote
    natural_credit: Decimal
    limit_credit: Decimal

    @property
    def symbols(self) -> List[str]:
        return [
            self.short_call.symbol,
            self.long_call.symbol,
            self.short_put.symbol,
            self.long_put.symbol,
        ]


class AlpacaAPIError(RuntimeError):
    pass


class AlpacaREST:
    def __init__(self) -> None:
        self.api_key = os.getenv("ALPACA_API_KEY", "").strip()
        self.secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()
        self.trading_url = os.getenv(
            "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"
        ).rstrip("/")
        self.data_url = os.getenv(
            "ALPACA_DATA_URL", "https://data.alpaca.markets"
        ).rstrip("/")
        self.timeout = float(os.getenv("ALPACA_HTTP_TIMEOUT", "20"))

        if not self.api_key or not self.secret_key:
            raise ValueError("Missing ALPACA_API_KEY or ALPACA_SECRET_KEY")
        if self.trading_url != "https://paper-api.alpaca.markets":
            raise ValueError(
                "This bot is paper-only; ALPACA_BASE_URL must be "
                "https://paper-api.alpaca.markets"
            )

        retries = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
        )
        self.session = requests.Session()
        self.session.headers.update(
            {
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
                "Accept": "application/json",
            }
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retries))

    def request(
        self,
        method: str,
        path: str,
        *,
        data_api: bool = False,
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        base_url = self.data_url if data_api else self.trading_url
        try:
            response = self.session.request(
                method,
                f"{base_url}{path}",
                params=params,
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise AlpacaAPIError(f"{method} {path} failed: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text[:1000]
            raise AlpacaAPIError(
                f"{method} {path} returned {response.status_code}: {detail}"
            )
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise AlpacaAPIError(f"{method} {path} returned invalid JSON") from exc

    def get(self, path: str, **kwargs: Any) -> Dict[str, Any]:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("POST", path, payload=payload)


class IronCondorBot:
    def __init__(self, state_path: Optional[Path] = None) -> None:
        self.api = AlpacaREST()
        self.state_path = state_path or Path(
            os.getenv("IRON_CONDOR_STATE_PATH", "iron_condor_state.json")
        )
        self.state = self._load_state()
        account = self._validate_account()
        log.info(
            "[ACCOUNT] Equity: $%.2f | Options buying power: $%.2f",
            float(account["equity"]),
            float(account.get("options_buying_power") or 0),
        )

    def _load_state(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            return {"trades": []}
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read state file {self.state_path}: {exc}") from exc
        if not isinstance(state.get("trades"), list):
            raise ValueError(f"Invalid state file {self.state_path}: missing trades list")
        return state

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary_path.write_text(
            json.dumps(self.state, indent=2, sort_keys=True), encoding="utf-8"
        )
        temporary_path.replace(self.state_path)

    def _validate_account(self) -> Dict[str, Any]:
        account = self.api.get("/v2/account")
        if account.get("status") != "ACTIVE" or account.get("trading_blocked"):
            raise ValueError("Alpaca paper account is not active for trading")
        level = int(account.get("options_trading_level") or 0)
        if level < 3:
            raise ValueError(
                f"Options trading level {level} cannot trade defined-risk spreads; level 3 required"
            )
        return account

    def get_market_time(self) -> datetime:
        return eastern_now()

    def _clock(self) -> Dict[str, Any]:
        return self.api.get("/v2/clock")

    def _in_window(self, start: str, end: str) -> bool:
        clock = self._clock()
        if not clock.get("is_open"):
            return False
        now = self.get_market_time().time()
        start_time = datetime.strptime(start, "%H:%M").time()
        end_time = datetime.strptime(end, "%H:%M").time()
        return start_time <= now <= end_time

    def is_entry_window(self) -> bool:
        return self._in_window(
            MARKET_HOURS["entry_window_start"], MARKET_HOURS["entry_window_end"]
        )

    def is_exit_window(self) -> bool:
        return self._in_window(
            MARKET_HOURS["exit_window_start"], MARKET_HOURS["exit_window_end"]
        )

    @staticmethod
    def parse_option_snapshot(symbol: str, snapshot: Dict[str, Any]) -> OptionQuote:
        match = OCC_PATTERN.match(symbol)
        if not match:
            raise ValueError(f"Invalid OCC option symbol: {symbol}")
        underlying, expiration_text, option_code, strike_text = match.groups()
        quote = snapshot.get("latestQuote") or {}
        greeks = snapshot.get("greeks") or {}
        bid = Decimal(str(quote.get("bp") or 0))
        ask = Decimal(str(quote.get("ap") or 0))
        delta = Decimal(str(greeks.get("delta") or 0))
        if bid < 0 or ask <= 0 or bid > ask or delta == 0:
            raise ValueError(f"Unusable quote for {symbol}")
        return OptionQuote(
            symbol=symbol,
            underlying=underlying,
            expiration=datetime.strptime(expiration_text, "%y%m%d").date(),
            option_type="call" if option_code == "C" else "put",
            strike=Decimal(strike_text) / Decimal("1000"),
            delta=delta,
            bid=bid,
            ask=ask,
        )

    def get_options_chain(
        self, symbol: str, expiration: Optional[date] = None
    ) -> List[OptionQuote]:
        today = self.get_market_time().date()
        params: Dict[str, Any] = {
            "feed": os.getenv("ALPACA_OPTIONS_FEED", "indicative"),
            "limit": 1000,
            "expiration_date_gte": (today + timedelta(days=ENTRY_RULES["dte_min"])).isoformat(),
            "expiration_date_lte": (today + timedelta(days=ENTRY_RULES["dte_max"])).isoformat(),
        }
        if expiration:
            params = {
                "feed": os.getenv("ALPACA_OPTIONS_FEED", "indicative"),
                "limit": 1000,
                "expiration_date": expiration.isoformat(),
            }

        snapshots: Dict[str, Any] = {}
        while True:
            response = self.api.get(
                f"/v1beta1/options/snapshots/{symbol}",
                data_api=True,
                params=params,
            )
            snapshots.update(response.get("snapshots") or {})
            page_token = response.get("next_page_token")
            if not page_token:
                break
            params["page_token"] = page_token

        chain: List[OptionQuote] = []
        for contract_symbol, snapshot in snapshots.items():
            try:
                chain.append(self.parse_option_snapshot(contract_symbol, snapshot))
            except ValueError:
                continue
        return chain

    @staticmethod
    def select_candidate(
        underlying: str, chain: Iterable[OptionQuote]
    ) -> Optional[IronCondorCandidate]:
        by_expiration: Dict[date, List[OptionQuote]] = {}
        for quote in chain:
            if quote.underlying == underlying:
                by_expiration.setdefault(quote.expiration, []).append(quote)

        candidates: List[IronCondorCandidate] = []
        midpoint_delta = (
            Decimal(str(ENTRY_RULES["short_delta_min"]))
            + Decimal(str(ENTRY_RULES["short_delta_max"]))
        ) / 2
        width = ENTRY_RULES["spread_width"]

        for expiration, quotes in by_expiration.items():
            calls = {quote.strike: quote for quote in quotes if quote.option_type == "call"}
            puts = {quote.strike: quote for quote in quotes if quote.option_type == "put"}
            short_calls = [
                quote
                for quote in calls.values()
                if Decimal(str(ENTRY_RULES["short_delta_min"]))
                <= quote.delta
                <= Decimal(str(ENTRY_RULES["short_delta_max"]))
            ]
            short_puts = [
                quote
                for quote in puts.values()
                if Decimal(str(ENTRY_RULES["short_delta_min"]))
                <= abs(quote.delta)
                <= Decimal(str(ENTRY_RULES["short_delta_max"]))
            ]
            if not short_calls or not short_puts:
                continue

            short_call = min(short_calls, key=lambda quote: abs(quote.delta - midpoint_delta))
            short_put = min(short_puts, key=lambda quote: abs(abs(quote.delta) - midpoint_delta))
            long_call = calls.get(short_call.strike + width)
            long_put = puts.get(short_put.strike - width)
            if not long_call or not long_put or short_put.strike >= short_call.strike:
                continue

            natural_credit = money(
                short_call.bid + short_put.bid - long_call.ask - long_put.ask
            )
            if not (
                ENTRY_RULES["target_credit_min"]
                <= natural_credit
                <= ENTRY_RULES["target_credit_max"]
            ):
                continue
            limit_credit = money(
                max(
                    ENTRY_RULES["target_credit_min"],
                    natural_credit - ENTRY_RULES["limit_order_discount"],
                )
            )
            candidates.append(
                IronCondorCandidate(
                    underlying,
                    expiration,
                    short_call,
                    long_call,
                    short_put,
                    long_put,
                    natural_credit,
                    limit_credit,
                )
            )

        if not candidates:
            return None
        target_credit = (
            ENTRY_RULES["target_credit_min"] + ENTRY_RULES["target_credit_max"]
        ) / 2
        return min(
            candidates,
            key=lambda candidate: (
                abs(candidate.limit_credit - target_credit), candidate.expiration
            ),
        )

    @staticmethod
    def _entry_payload(candidate: IronCondorCandidate, contracts: int) -> Dict[str, Any]:
        legs = [
            (candidate.short_call.symbol, "sell", "sell_to_open"),
            (candidate.long_call.symbol, "buy", "buy_to_open"),
            (candidate.short_put.symbol, "sell", "sell_to_open"),
            (candidate.long_put.symbol, "buy", "buy_to_open"),
        ]
        return {
            "order_class": "mleg",
            "qty": str(contracts),
            "type": "limit",
            "limit_price": str(-candidate.limit_credit),
            "time_in_force": "day",
            "client_order_id": f"ic-entry-{candidate.underlying}-{int(datetime.now().timestamp())}",
            "legs": [
                {
                    "symbol": symbol,
                    "ratio_qty": "1",
                    "side": side,
                    "position_intent": intent,
                }
                for symbol, side, intent in legs
            ],
        }

    @staticmethod
    def _close_payload(trade: Dict[str, Any], close_debit: Decimal) -> Dict[str, Any]:
        short_call, long_call, short_put, long_put = trade["symbols"]
        legs = [
            (short_call, "buy", "buy_to_close"),
            (long_call, "sell", "sell_to_close"),
            (short_put, "buy", "buy_to_close"),
            (long_put, "sell", "sell_to_close"),
        ]
        return {
            "order_class": "mleg",
            "qty": str(trade["contracts"]),
            "type": "limit",
            "limit_price": str(money(close_debit)),
            "time_in_force": "day",
            "client_order_id": f"ic-exit-{trade['underlying']}-{int(datetime.now().timestamp())}",
            "legs": [
                {
                    "symbol": symbol,
                    "ratio_qty": "1",
                    "side": side,
                    "position_intent": intent,
                }
                for symbol, side, intent in legs
            ],
        }

    def _has_open_trade(self, underlying: str) -> bool:
        return any(
            trade["underlying"] == underlying
            and trade["status"] in {"entry_pending", "active", "exit_pending"}
            for trade in self.state["trades"]
        )

    def place_entry_order(self, candidate: IronCondorCandidate) -> str:
        account = self._validate_account()
        contracts = int(ENTRY_RULES["position_size"])
        risk = money(
            (ENTRY_RULES["spread_width"] - candidate.limit_credit)
            * Decimal("100")
            * contracts
        )
        equity = Decimal(str(account["equity"]))
        options_buying_power = Decimal(str(account.get("options_buying_power") or 0))
        if risk > equity * ENTRY_RULES["max_risk_pct"]:
            raise ValueError(f"Max loss ${risk} exceeds the 2% account risk limit")
        if risk > options_buying_power:
            raise ValueError(f"Max loss ${risk} exceeds options buying power")
        if self._has_open_trade(candidate.underlying):
            raise ValueError(f"Open iron condor already exists for {candidate.underlying}")

        payload = self._entry_payload(candidate, contracts)
        order = self.api.post("/v2/orders", payload)
        order_id = order.get("id")
        if not order_id:
            raise AlpacaAPIError("Entry order response did not contain an order ID")
        self.state["trades"].append(
            {
                "underlying": candidate.underlying,
                "expiration": candidate.expiration.isoformat(),
                "symbols": candidate.symbols,
                "contracts": contracts,
                "entry_credit": str(candidate.limit_credit),
                "max_loss": str(risk),
                "entry_order_id": order_id,
                "exit_order_id": None,
                "status": "entry_pending",
                "created_at": eastern_now().isoformat(),
            }
        )
        self._save_state()
        log.info(
            "[ENTRY] Submitted %s iron condor for $%s credit: %s",
            candidate.underlying,
            candidate.limit_credit,
            order_id,
        )
        return order_id

    def _reconcile_orders(self) -> None:
        changed = False
        for trade in self.state["trades"]:
            if trade["status"] == "entry_pending":
                order = self.api.get(f"/v2/orders/{trade['entry_order_id']}")
                if order.get("status") == "filled":
                    trade["status"] = "active"
                    trade["filled_at"] = order.get("filled_at")
                    changed = True
                elif order.get("status") in FINAL_ORDER_STATUSES:
                    trade["status"] = f"entry_{order['status']}"
                    changed = True
            elif trade["status"] == "exit_pending":
                order = self.api.get(f"/v2/orders/{trade['exit_order_id']}")
                if order.get("status") == "filled":
                    trade["status"] = "closed"
                    trade["closed_at"] = order.get("filled_at")
                    changed = True
                elif order.get("status") in FINAL_ORDER_STATUSES:
                    trade["status"] = "active"
                    trade["exit_order_id"] = None
                    changed = True
        if changed:
            self._save_state()

    def _closing_debit(self, trade: Dict[str, Any]) -> Decimal:
        chain = self.get_options_chain(
            trade["underlying"], date.fromisoformat(trade["expiration"])
        )
        quotes = {quote.symbol: quote for quote in chain}
        missing = set(trade["symbols"]) - quotes.keys()
        if missing:
            raise ValueError(f"Missing current quotes for: {', '.join(sorted(missing))}")
        short_call, long_call, short_put, long_put = (
            quotes[symbol] for symbol in trade["symbols"]
        )
        return money(
            short_call.ask + short_put.ask - long_call.bid - long_put.bid
        )

    def check_exit_conditions(self) -> None:
        self._reconcile_orders()
        for trade in self.state["trades"]:
            if trade["status"] != "active":
                continue
            entry_credit = Decimal(trade["entry_credit"])
            close_debit = self._closing_debit(trade)
            days_to_expiry = (
                date.fromisoformat(trade["expiration"]) - self.get_market_time().date()
            ).days
            profit = entry_credit - close_debit
            reason: Optional[str] = None
            if profit >= entry_credit * EXIT_RULES["profit_target_pct"]:
                reason = "profit_target"
            elif -profit >= entry_credit * EXIT_RULES["stop_loss_pct"]:
                reason = "stop_loss"
            elif days_to_expiry <= EXIT_RULES["days_to_expiry_exit"]:
                reason = "expiration"

            log.info(
                "[EXIT] %s entry=$%s close=$%s P&L=$%s DTE=%d",
                trade["underlying"],
                entry_credit,
                close_debit,
                money(profit * Decimal("100") * trade["contracts"]),
                days_to_expiry,
            )
            if not reason:
                continue
            order = self.api.post("/v2/orders", self._close_payload(trade, close_debit))
            if not order.get("id"):
                raise AlpacaAPIError("Exit order response did not contain an order ID")
            trade["exit_order_id"] = order["id"]
            trade["exit_reason"] = reason
            trade["status"] = "exit_pending"
            self._save_state()
            log.info("[EXIT] Submitted %s close order: %s", reason, order["id"])

    def scan_for_entry(self) -> Optional[str]:
        self._reconcile_orders()
        for underlying in ENTRY_RULES["underlyings"]:
            if self._has_open_trade(underlying):
                continue
            candidate = self.select_candidate(underlying, self.get_options_chain(underlying))
            if candidate:
                return self.place_entry_order(candidate)
            log.info("[SCAN] No rule-compliant candidate for %s", underlying)
        return None

    def run_daily_cycle(self, force_entry: bool = False, force_exit: bool = False) -> None:
        self._reconcile_orders()
        if force_entry or self.is_entry_window():
            self.scan_for_entry()
        if force_exit or self.is_exit_window():
            self.check_exit_conditions()

    def generate_daily_report(self) -> Dict[str, Any]:
        account = self._validate_account()
        statuses: Dict[str, int] = {}
        for trade in self.state["trades"]:
            statuses[trade["status"]] = statuses.get(trade["status"], 0) + 1
        report = {
            "timestamp": eastern_now().isoformat(),
            "account_equity": float(account["equity"]),
            "options_buying_power": float(account.get("options_buying_power") or 0),
            "trade_statuses": statuses,
        }
        log.info("[REPORT] %s", json.dumps(report, sort_keys=True))
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force-entry", action="store_true", help="scan outside entry window")
    parser.add_argument("--force-exit", action="store_true", help="check exits outside exit window")
    arguments = parser.parse_args()
    try:
        bot = IronCondorBot()
        bot.run_daily_cycle(arguments.force_entry, arguments.force_exit)
        bot.generate_daily_report()
        return 0
    except Exception:
        log.exception("[BOT] Fatal error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
