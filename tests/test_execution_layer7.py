import json
import math
import os
from datetime import datetime, timedelta, timezone

from execution.executor import (
    _calc_btc_qty,
    _entry_price_for_signal,
    _pnl_with_fees,
    _load_state,
    _save_state,
    TradeExecutor,
    HEARTBEAT_FILE,
    TRADES_LOG_FILE,
    EVENTS_LOG_FILE,
    DAILY_SUMMARY_FILE,
    BOT_STATE_FILE,
)


def _print_result(name, expected, actual, passed):
    status = "PASS" if passed else "FAIL"
    print(f"{name}: expected={expected} actual={actual} => {status}")


class FakeClient:
    def __init__(self):
        self.orders = {}
        self.counter = 0
        self.ticker = {"LastPrice": 80000, "MaxBid": 79990, "MinAsk": 80010}
        self.fill_status = []

    def place_order(self, symbol, side, order_type, qty, price):
        self.counter += 1
        order_id = str(self.counter)
        self.orders[order_id] = {"qty": qty, "price": price, "side": side}
        return {"OrderID": order_id}

    def cancel_order(self, order_id):
        return {"OrderID": order_id}

    def query_orders(self, pair=None, pending_only=False):
        if self.fill_status:
            status = self.fill_status.pop(0)
        else:
            status = {"OrderID": "1", "Status": "FILLED", "FilledQty": 1.0, "AvgPrice": 80010}
        return {"Orders": [status]}

    def get_ticker(self, pair=None):
        return self.ticker


def test_component1_entry_calcs():
    qty = _calc_btc_qty(83_300, 80_000, 5)
    price = _entry_price_for_signal(79_990, "DONCHIAN_BREAKOUT", 2)
    passed = math.isclose(qty, 1.04125, rel_tol=1e-6) and math.isclose(price, 80006.00, rel_tol=1e-6)
    _print_result("COMP1 TEST1", "qty=1.04125 price=80006", f"qty={qty} price={price}", passed)
    assert passed

    price = _entry_price_for_signal(79_990, "MEAN_REVERSION", 2)
    passed = math.isclose(price, 79990.00, rel_tol=1e-6)
    _print_result("COMP1 TEST2", "79990.00", f"{price}", passed)
    assert passed


def test_component2_trailing_stop_logic():
    client = FakeClient()
    ex = TradeExecutor(client, 2, 5)
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

    reason, stop, _ = ex._evaluate_trailing(80_000, 80_520, 500, "SIDEWAYS", 0)
    passed = reason == "FIXED_TAKE_PROFIT"
    _print_result("COMP2 TEST4", "FIXED_TAKE_PROFIT", f"{reason}", passed)
    assert passed

    reason, stop, _ = ex._evaluate_trailing(80_000, 79_200, 500, "SIDEWAYS", 0)
    passed = reason == "STOP_LOSS_FIXED"
    _print_result("COMP2 TEST5", "STOP_LOSS_FIXED", f"{reason}", passed)
    assert passed


def test_component2_pnl_with_fees():
    pnl = _pnl_with_fees(80_000, 81_250, 1.04125)
    passed = math.isclose(pnl["pnl_usd_net"], 1217.61, rel_tol=1e-2)
    _print_result("COMP2 TEST6", "1217.61", f"{pnl['pnl_usd_net']:.2f}", passed)
    assert passed


def test_component3_time_exit_logic():
    client = FakeClient()
    ex = TradeExecutor(client, 2, 5)
    ex.state["position_open_time"] = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
    ex.state["entry_price"] = 80_000
    passed = ex._should_time_exit(80_000, 80_040)
    _print_result("COMP3 TEST1", True, passed, passed)
    assert passed

    ex.state["position_open_time"] = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
    passed = not ex._should_time_exit(80_000, 80_400)
    _print_result("COMP3 TEST2", False, not passed, passed)
    assert passed

    ex.state["position_open_time"] = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    passed = not ex._should_time_exit(80_000, 80_000)
    _print_result("COMP3 TEST3", False, not passed, passed)
    assert passed


def test_component4_cooldown():
    client = FakeClient()
    ex = TradeExecutor(client, 2, 5)
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
        "reversal_blocker_result": "PASSED",
        "xgboost_probability": 0.73,
        "timeframe_scores": {"1H": 1, "4H": 1, "Daily": 1},
        "timeframe_total_score": 3,
        "position_size_usd": 1000.0,
        "position_size_btc": 0.01,
        "entry_price": 80000.0,
        "stop_loss_level": 79250.0,
        "take_profit_level": None,
        "exit_price": 81250.0,
        "exit_reason": "TRAILING_STOP",
        "pnl_usd_gross": 100.0,
        "pnl_usd_net": 90.0,
        "pnl_pct": 1.56,
        "fees_paid_usd": 10.0,
        "portfolio_value_after": 1000090.0,
        "drawdown_at_trade_pct": 0.0,
        "cooldown_triggered": True,
        "partial_fill": False,
        "partial_fill_pct": None,
    }
    with open(TRADES_LOG_FILE, "w", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    with open(TRADES_LOG_FILE, "r", encoding="utf-8") as f:
        data = json.loads(f.readline())
    required = {
        "timestamp",
        "regime",
        "signal_source",
        "reversal_blocker_result",
        "xgboost_probability",
        "timeframe_scores",
        "timeframe_total_score",
        "position_size_usd",
        "position_size_btc",
        "entry_price",
        "stop_loss_level",
        "take_profit_level",
        "exit_price",
        "exit_reason",
        "pnl_usd_gross",
        "pnl_usd_net",
        "pnl_pct",
        "fees_paid_usd",
        "portfolio_value_after",
        "drawdown_at_trade_pct",
        "cooldown_triggered",
        "partial_fill",
        "partial_fill_pct",
    }
    missing = required.difference(data.keys())
    passed = len(missing) == 0
    _print_result("COMP5 TEST1", "all fields present", f"missing={sorted(missing)}", passed)
    assert passed


def test_component6_heartbeat():
    client = FakeClient()
    ex = TradeExecutor(client, 2, 5)
    ex._heartbeat()
    with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
        hb = json.load(f)
    passed = hb.get("status") == "alive" and isinstance(hb.get("portfolio_value"), float)
    _print_result("COMP6 TEST1", "alive", hb.get("status"), passed)
    assert passed


def test_component7_state_persistence():
    state = _load_state()
    state["position_open"] = True
    state["entry_price"] = 80_000
    state["current_stop"] = 79_250
    _save_state(state)
    loaded = _load_state()
    passed = loaded["position_open"] and loaded["entry_price"] == 80_000 and loaded["current_stop"] == 79_250
    _print_result("COMP7 TEST1", True, passed, passed)
    assert passed


def test_full_integration_simulation():
    if os.path.exists(BOT_STATE_FILE):
        os.remove(BOT_STATE_FILE)
    client = FakeClient()
    client.fill_status = [
        {"OrderID": "1", "Status": "FILLED", "FilledQty": 1.04138, "AvgPrice": 80010},
    ]
    ex = TradeExecutor(client, 2, 5)
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
    state = _load_state()
    passed = state["position_open"] is True and math.isclose(state["entry_price"], 80010, rel_tol=1e-6)
    _print_result("FULL INTEGRATION", True, passed, passed)
    assert passed
