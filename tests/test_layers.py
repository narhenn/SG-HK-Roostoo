import math

from data.candle_builder import CandleBuilder
from strategy.regime import detect_regime
from strategy.signals import generate_signal
from strategy.reversal_blocker import check_reversal_block, reset_cooldown
from strategy.timeframe import check_timeframe
from strategy.ml_model import engineer_features, xgboost_confirm


def _load_candles():
    cb = CandleBuilder()
    loaded = cb.bootstrap()
    assert loaded, "Historical CSV not found for tests"
    return cb


def test_layer1_regime_returns_valid_value():
    cb = _load_candles()
    df_1h = cb.get_df("1h").tail(200)

    regime = detect_regime(df_1h, fear_greed=50, funding_rate=0.0, breadth=0.5)
    assert regime in {"TRENDING", "SIDEWAYS", "VOLATILE"}


def test_layer2_signal_returns_valid_structure():
    cb = _load_candles()
    df_1h = cb.get_df("1h").tail(200)

    regime = detect_regime(df_1h, fear_greed=50, funding_rate=0.0, breadth=0.5)
    signal = generate_signal(df_1h, regime)

    assert isinstance(signal, dict)
    assert signal.get("direction") in {"BUY", "SELL", "HOLD"}
    assert isinstance(signal.get("source"), str)


def test_layer3_reversal_blocker_extreme_move_blocks():
    reset_cooldown()

    prices = [79000, 79200, 80500, 81000, 81500]
    volumes = [100, 105, 98, 102, 101]
    spread = 0.0001

    result = check_reversal_block(prices, volumes, spread, "BUY")
    assert result["decision"] == "BLOCK"
    assert result["check1_extreme_move"] is True


def test_layer4_timeframe_returns_expected_shape():
    cb = _load_candles()
    df_1h = cb.get_df("1h").tail(200)
    df_4h = cb.get_df("4h")
    df_daily = cb.get_df("daily")

    regime = detect_regime(df_1h, fear_greed=50, funding_rate=0.0, breadth=0.5)
    tf = check_timeframe(df_1h, df_4h, df_daily, regime=regime)

    assert set(tf.keys()) == {"pass", "score", "multiplier", "scores"}
    assert isinstance(tf["pass"], bool)
    assert isinstance(tf["score"], int)
    assert isinstance(tf["multiplier"], float)
    assert set(tf["scores"].keys()) == {"1h", "4h", "daily"}


def test_layer5_xgboost_prob_in_bounds():
    cb = _load_candles()
    df_1h = cb.get_df("1h").tail(200)

    features = engineer_features(df_1h)
    assert isinstance(features, dict)
    assert len(features) > 0
    for value in features.values():
        assert isinstance(value, (int, float))
        assert not math.isnan(float(value))

    prob = xgboost_confirm(features)
    assert isinstance(prob, float)
    assert 0.0 <= prob <= 1.0
