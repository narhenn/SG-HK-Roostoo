import math

from risk import position_sizer as ps
from data.state import default_state


def _print_result(name, expected, actual, passed):
    status = "PASS" if passed else "FAIL"
    print(f"{name}: expected={expected} actual={actual} => {status}")


def _dummy_save_state(_state):
    return None


def test_step_a_quarter_kelly():
    state = default_state()

    # 20 trades: 12 wins at +1.5%, 8 losses at -1.0%
    trade_history = [{"pnl_pct": 0.015}] * 12 + [{"pnl_pct": -0.01}] * 8
    frac, size = ps._quarter_kelly_size(trade_history, 1_000_000)
    expected_frac = 0.0833
    expected_size = 83_300
    passed = math.isclose(frac, expected_frac, rel_tol=1e-3) and math.isclose(size, expected_size, rel_tol=1e-3)
    _print_result("UNIT TEST A1", f"{expected_frac:.4f}, {expected_size}", f"{frac:.4f}, {size:.0f}", passed)
    assert passed

    # Cold start defaults
    trade_history = [{"pnl_pct": 0.01}] * 5
    frac, size = ps._quarter_kelly_size(trade_history, 1_000_000)
    expected_frac = 0.0673
    expected_size = 67_300
    passed = math.isclose(frac, expected_frac, rel_tol=1e-3) and math.isclose(size, expected_size, rel_tol=1e-3)
    _print_result("UNIT TEST A2", f"{expected_frac:.4f}, {expected_size}", f"{frac:.4f}, {size:.0f}", passed)
    assert passed

    # Bad streak -> zero
    trade_history = [{"pnl_pct": 0.01}] * 6 + [{"pnl_pct": -0.02}] * 14
    frac, size = ps._quarter_kelly_size(trade_history, 1_000_000)
    passed = frac <= 0 and size == 0
    _print_result("UNIT TEST A3", "0", f"{frac:.4f}, {size:.0f}", passed)
    assert passed


def test_step_b_regime_multiplier():
    base = 83_300
    for regime, expected in [("TRENDING", 83_300), ("SIDEWAYS", 41_650), ("VOLATILE", 8_330)]:
        actual = base * ps._regime_multiplier(regime)
        passed = math.isclose(actual, expected, rel_tol=1e-3)
        _print_result(f"UNIT TEST B {regime}", expected, f"{actual:.0f}", passed)
        assert passed


def test_step_c_timeframe_multiplier():
    base = 83_300
    actual_3 = base * ps._timeframe_multiplier(3)
    actual_2 = base * ps._timeframe_multiplier(2)
    passed_3 = math.isclose(actual_3, 83_300, rel_tol=1e-3)
    passed_2 = math.isclose(actual_2, 41_650, rel_tol=1e-3)
    _print_result("UNIT TEST C score=3", 83_300, f"{actual_3:.0f}", passed_3)
    _print_result("UNIT TEST C score=2", 41_650, f"{actual_2:.0f}", passed_2)
    assert passed_3 and passed_2


def test_step_d_drawdown_throttle():
    state = default_state()

    cap, allowed = ps._drawdown_cap(1_000_000, 975_000, 75, 3, True, state)
    expected = 243_750
    passed = allowed and math.isclose(cap, expected, rel_tol=1e-6)
    _print_result("UNIT TEST D1", expected, f"{cap:.0f}", passed)
    assert passed

    cap, allowed = ps._drawdown_cap(1_000_000, 975_000, 65, 3, True, state)
    passed = (not allowed) and cap == 0
    _print_result("UNIT TEST D2", 0, cap, passed)
    assert passed

    cap, allowed = ps._drawdown_cap(1_000_000, 935_000, 82, 3, True, state)
    expected = 140_250
    passed = allowed and math.isclose(cap, expected, rel_tol=1e-6)
    _print_result("UNIT TEST D3", expected, f"{cap:.0f}", passed)
    assert passed

    state = default_state()
    cap, allowed = ps._drawdown_cap(1_000_000, 915_000, 90, 3, True, state)
    passed = (not allowed) and cap == 0 and state.get("halt_until")
    _print_result("UNIT TEST D4", 0, cap, passed)
    assert passed

    state = default_state()
    cap, allowed = ps._drawdown_cap(1_000_000, 895_000, 90, 3, True, state)
    expected = 44_750
    passed = allowed and math.isclose(cap, expected, rel_tol=1e-6)
    _print_result("UNIT TEST D5", expected, f"{cap:.0f}", passed)
    assert passed


def test_step_e_hard_limits():
    hard = ps._hard_limits(1_000_000, 500, 80_000)
    expected = 350_000
    passed = math.isclose(hard["hard_cap_35pct"], expected, rel_tol=1e-6)
    _print_result("UNIT TEST E1", expected, f"{hard['hard_cap_35pct']:.0f}", passed)
    assert passed

    hard = ps._hard_limits(1_000_000, 2_000, 80_000)
    expected = 133_333
    passed = math.isclose(hard["volatility_size_usd"], expected, rel_tol=1e-3)
    _print_result("UNIT TEST E2", expected, f"{hard['volatility_size_usd']:.0f}", passed)
    assert passed


def test_full_integration_scenarios():
    state = default_state()

    trade_history = [{"pnl_pct": 0.015}] * 12 + [{"pnl_pct": -0.01}] * 8
    size = ps.compute_position_size(
        current_capital=1_000_000,
        peak_capital=1_000_000,
        trade_history=trade_history,
        regime="TRENDING",
        timeframe_score=3,
        signal_score=90,
        atr_usd=500,
        btc_price=80_000,
        current_position_open=False,
        rolling_sharpe_3day=0.3,
        state=state,
        save_state_fn=_dummy_save_state,
    )
    expected = 83_300
    passed = math.isclose(size, expected, rel_tol=1e-3)
    _print_result("INTEGRATION 1", expected, f"{size:.0f}", passed)
    assert passed

    state = default_state()
    size = ps.compute_position_size(
        current_capital=970_000,
        peak_capital=1_000_000,
        trade_history=trade_history,
        regime="SIDEWAYS",
        timeframe_score=2,
        signal_score=72,
        atr_usd=600,
        btc_price=80_000,
        current_position_open=False,
        rolling_sharpe_3day=0.3,
        state=state,
        save_state_fn=_dummy_save_state,
    )
    expected = 20_203
    passed = math.isclose(size, expected, rel_tol=1e-3)
    _print_result("INTEGRATION 2", expected, f"{size:.0f}", passed)
    assert passed

    state = default_state()
    size = ps.compute_position_size(
        current_capital=915_000,
        peak_capital=1_000_000,
        trade_history=trade_history,
        regime="TRENDING",
        timeframe_score=3,
        signal_score=90,
        atr_usd=500,
        btc_price=80_000,
        current_position_open=False,
        rolling_sharpe_3day=0.3,
        state=state,
        save_state_fn=_dummy_save_state,
    )
    passed = size == 0
    _print_result("INTEGRATION 3", 0, f"{size:.0f}", passed)
    assert passed


def test_s3_partial_fill_handling():
    state = default_state()
    result = ps.handle_partial_fill(ordered_qty=1.0, filled_qty=0.6, state=state, save_state_fn=_dummy_save_state)
    passed = result["accepted"] is True
    _print_result("UNIT TEST S3-1", "accepted", result["reason"], passed)
    assert passed

    state = default_state()
    result = ps.handle_partial_fill(ordered_qty=1.0, filled_qty=0.4, state=state, save_state_fn=_dummy_save_state)
    passed = result["accepted"] is False and state.get("cooldown_until")
    _print_result("UNIT TEST S3-2", "rejected", result["reason"], passed)
    assert passed


def test_s6_kill_switch():
    trade_history = [{"pnl_pct": 0.01}] * 20
    state = default_state()
    size = ps.compute_position_size(
        current_capital=840_000,
        peak_capital=1_000_000,
        trade_history=trade_history,
        regime="TRENDING",
        timeframe_score=3,
        signal_score=90,
        atr_usd=500,
        btc_price=80_000,
        current_position_open=False,
        rolling_sharpe_3day=0.3,
        state=state,
        save_state_fn=_dummy_save_state,
    )
    passed = size == 0 and state.get("halt_until")
    _print_result("UNIT TEST S6-1", 0, f"{size:.0f}", passed)
    assert passed

    state = default_state()
    size = ps.compute_position_size(
        current_capital=920_000,
        peak_capital=1_000_000,
        trade_history=trade_history,
        regime="TRENDING",
        timeframe_score=3,
        signal_score=90,
        atr_usd=500,
        btc_price=80_000,
        current_position_open=False,
        rolling_sharpe_3day=-0.7,
        state=state,
        save_state_fn=_dummy_save_state,
    )
    passed = size == 0 and state.get("halt_until")
    _print_result("UNIT TEST S6-2", 0, f"{size:.0f}", passed)
    assert passed

    state = default_state()
    size = ps.compute_position_size(
        current_capital=960_000,
        peak_capital=1_000_000,
        trade_history=trade_history,
        regime="TRENDING",
        timeframe_score=3,
        signal_score=90,
        atr_usd=500,
        btc_price=80_000,
        current_position_open=False,
        rolling_sharpe_3day=0.3,
        state=state,
        save_state_fn=_dummy_save_state,
    )
    passed = size > 0
    _print_result("UNIT TEST S6-3", "non-zero", f"{size:.0f}", passed)
    assert passed
