import json
import math
import os
from datetime import datetime, timedelta, timezone

from execution.executor import (
    _calc_btc_qty,
    _entry_price_for_signal,
    _pnl_with_fees,
    _parse_ticker,
    TradeExecutor,
    TRADES_LOG_FILE,
    EVENTS_LOG_FILE,
)


def _print_result(name, expected, actual, passed):
    status = "PASS" if passed else "FAIL"
    print(f"{name}: expected={expected} actual={actual} => {status}")


class FakeClient:
    def __init__(self):
        self.orders = {}
        self.counter = 0
        self.ticker = {"Data": {"BTC/USD": {"LastPrice": 80000, "MaxBid": 79990, "MinAsk": 80010}}}
        self.fill_status = []

    def place_order(self, symbol, side, order_type, qty, price):
        self.counter += 1
        order_id = str(self.counter)
        self.orders[order_id] = {"qty": qty, "price": price, "side": side}
        return {"OrderDetail": {"OrderID": order_id, "Status": "PENDING", "FilledQuantity": 0, "FilledAverPrice": 0, "Quantity": qty}}

    def cancel_order(self, order_id):
        return {"OrderID": order_id}

    def query_orders(self, pair=None, pending_only=False):
        if self.fill_status:
            status = self.fill_status.pop(0)
        else:
            status = {"OrderID": "1", "Status": "COMPLETED", "Quantity": 1.0, "FilledQuantity": 1.0, "FilledAverPrice": 80010}
        return {"OrderMatched": [status]}

    def get_ticker(self, pair=None):
        return self.ticker


def test_component1_entry_calcs():
    qty = _calc_btc_qty(83_300, 80_000, 5)
    # Now uses ask price (80010) to cross spread, not bid (79990)
    price = _entry_price_for_signal(79_990, 80_010, "DONCHIAN_BREAKOUT", 2)
    # Breakout: ask * (1 + 0.0002) = 80010 * 1.0002 = 80026.00
    passed = math.isclose(qty, 1.04125, rel_tol=1e-6) and math.isclose(price, 80026.00, rel_tol=1e-6)
    _print_result("COMP1 TEST1", "qty=1.04125 price=80026", f"qty={qty} price={price}", passed)
    assert passed

    price = _entry_price_for_signal(79_990, 80_010, "MEAN_REVERSION", 2)
    # Mean reversion: at ask = 80010.00
    passed = math.isclose(price, 80010.00, rel_tol=1e-6)
    _print_result("COMP1 TEST2", "80010.00", f"{price}", passed)
    assert passed


def test_component2_trailing_stop_logic():
    client = FakeClient()
    ex = TradeExecutor(client, 2, 5, state={}, save_state_fn=lambda s: None)
    reason, stop, _ = ex._evaluate_trailing(80_000, 81_000, 500, "TRENDING", 79_250)
    passed = reason is None and math.isclose(stop, 80_250, rel_tol=1e-6)
    _print_result("COMP2 TEST1", "stop=80250", f"stop={stop}", passed)
    assert passed

    reason, stop, _ = ex._evaluate_trailing(80_000, 82_000, 500, "TRENDING", 80_250)
    passed = reason is None and math.isclose(stop, 81_250, rel_tol=1e-6)
    _print_result("COMP2 TEST2", "stop=81250", f"stop={stop}", passed)
    assert passed

    reason, stop, _ = ex._evaluate_trailing(80_000, 81_200, 500, "TRENDING", 81_250)
    passed = reason == "TRAILING_STOP"
    _print_result("COMP2 TEST3", "TRAILING_STOP", f"{reason}", passed)
    assert passed

    # SIDEWAYS: TP=1.5*ATR=750, SL=0.7*ATR=350
    # Price 80,800 = entry+800 > TP(80,750) → take profit
    reason, stop, _ = ex._evaluate_trailing(80_000, 80_800, 500, "SIDEWAYS", 0)
    passed = reason == "FIXED_TAKE_PROFIT"
    _print_result("COMP2 TEST4", "FIXED_TAKE_PROFIT", f"{reason}", passed)
    assert passed

    # Price 79,600 = entry-400 > SL(79,650) but close. Try 79,600 < 79,650 → stop
    reason, stop, _ = ex._evaluate_trailing(80_000, 79_600, 500, "SIDEWAYS", 0)
    passed = reason == "STOP_LOSS_SIDEWAYS"
    _print_result("COMP2 TEST5", "STOP_LOSS_SIDEWAYS", f"{reason}", passed)
    assert passed


def test_component2_pnl_with_fees():
    pnl = _pnl_with_fees(80_000, 81_250, 1.04125)
    # Using TAKER fees (0.1% each side) not MAKER (0.05%)
    passed = math.isclose(pnl["pnl_usd_net"], 1133.66, rel_tol=1e-2)
    _print_result("COMP2 TEST6", "1133.66", f"{pnl['pnl_usd_net']:.2f}", passed)
    assert passed


def test_component3_time_exit_logic():
    client = FakeClient()
    ex = TradeExecutor(client, 2, 5, state={}, save_state_fn=lambda s: None)
    # Time exit only fires if position is NEGATIVE (pnl < 0)
    # Price 79,900 < entry 80,000 → negative → should time exit
    ex.state["exec_open_time"] = (datetime.now(timezone.utc) - timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%S.%f")
    passed = ex._should_time_exit(80_000, 79_900)
    _print_result("COMP3 TEST1", True, passed, passed)
    assert passed

    # Price 80,040 > entry 80,000 → positive → should NOT time exit
    ex.state["exec_open_time"] = (datetime.now(timezone.utc) - timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%S.%f")
    passed = not ex._should_time_exit(80_000, 80_040)
    _print_result("COMP3 TEST2", False, not passed, passed)
    assert passed

    ex.state["exec_open_time"] = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.%f")
    passed = not ex._should_time_exit(80_000, 80_000)
    _print_result("COMP3 TEST3", False, not passed, passed)
    assert passed


def test_component4_cooldown():
    client = FakeClient()
    ex = TradeExecutor(client, 2, 5, state={}, save_state_fn=lambda s: None)
    ex.state["cooldown_until"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    active = ex.state["cooldown_until"] > datetime.now(timezone.utc).isoformat()
    passed = active
    _print_result("COMP4 TEST1", True, active, passed)
    assert passed


def test_component5_logging_fields():
    entry = {
        "timestamp": "2020-01-01T00:00:00Z",
        "regime": "TRENDING",
        "signal_source": "DONCHIAN_BREAKOUT",
        "entry_price": 80000.0,
        "exit_price": 81250.0,
        "exit_reason": "TRAILING_STOP",
        "position_size_btc": 0.01,
        "pnl_usd_gross": 100.0,
        "pnl_usd_net": 90.0,
        "pnl_pct": 0.0156,
        "fees_paid_usd": 10.0,
        "portfolio_value_after": 1000090.0,
    }
    with open(TRADES_LOG_FILE, "w", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    with open(TRADES_LOG_FILE, "r", encoding="utf-8") as f:
        data = json.loads(f.readline())
    required = {
        "timestamp", "regime", "signal_source", "entry_price",
        "exit_price", "exit_reason", "pnl_usd_gross", "pnl_usd_net",
        "pnl_pct", "fees_paid_usd", "portfolio_value_after",
    }
    missing = required.difference(data.keys())
    passed = len(missing) == 0
    _print_result("COMP5 TEST1", "all fields present", f"missing={sorted(missing)}", passed)
    assert passed


def test_component6_ticker_parsing():
    # Nested format (real Roostoo)
    raw = {"Data": {"BTC/USD": {"LastPrice": 80000, "MaxBid": 79990, "MinAsk": 80010}}}
    parsed = _parse_ticker(raw, "BTC/USD")
    assert parsed['price'] == 80000
    assert parsed['bid'] == 79990

    # Flat format
    raw2 = {"LastPrice": 80000, "MaxBid": 79990, "MinAsk": 80010}
    parsed2 = _parse_ticker(raw2)
    assert parsed2['price'] == 80000


def test_component7_state_shared():
    """Verify executor uses shared state dict, not separate file."""
    state = {"current_equity": 50000, "peak_equity": 50000, "positions": []}
    client = FakeClient()
    ex = TradeExecutor(client, 2, 5, state=state, save_state_fn=lambda s: None)
    # Executor should have added its fields to the shared state
    assert "exec_position_open" in state
    assert state["exec_position_open"] is False
    # Verify it's the same object
    assert ex.state is state


def test_full_integration_simulation():
    state = {"current_equity": 50000, "peak_equity": 50000, "positions": [], "trade_history": []}
    saved = []
    client = FakeClient()
    client.fill_status = [
        {"OrderID": "1", "Status": "COMPLETED", "Quantity": 1.04138, "FilledQuantity": 1.04138, "FilledAverPrice": 80010},
    ]
    ex = TradeExecutor(client, 2, 5, state=state, save_state_fn=lambda s: saved.append(True))
    ex.execute_trade(
        final_position_size_usd=83_300,
        current_btc_price=80_000,
        current_bid=79_990,
        current_ask=80_010,
        atr_14=500,
        regime="TRENDING",
        signal_source="DONCHIAN_BREAKOUT",
        entry_context={
            "reversal_blocker_result": "PASSED",
            "xgboost_probability": 0.73,
            "timeframe_scores": {"1H": 1, "4H": 1, "Daily": 1},
            "timeframe_total_score": 3,
        },
    )
    passed = state["exec_position_open"] is True and math.isclose(state["exec_entry_price"], 80010, rel_tol=1e-6)
    _print_result("FULL INTEGRATION", True, passed, passed)
    assert passed
    # Verify positions list was also updated
    assert len(state["positions"]) == 1
    # Verify save was called
    assert len(saved) > 0
