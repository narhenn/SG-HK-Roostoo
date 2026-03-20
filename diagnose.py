#!/usr/bin/env python3
"""
Pipeline Diagnostic Tool — read-only, safe to run while bot is live.
Shows exactly where the pipeline would block and why.

Usage: python3 diagnose.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data.candle_builder import CandleBuilder
from data.fetchers import fetch_fear_greed, fetch_funding_rate, fetch_market_breadth
from strategy.regime import detect_regime, calculate_atr, calculate_adx, calculate_bb_width
from strategy.signals import generate_signal
from strategy.timeframe import check_timeframe
from risk.position_sizer import compute_position_size, _timeframe_multiplier
from config import (
    XGBOOST_MIN_PROBABILITY, RSI_OVERSOLD, RSI_OVERBOUGHT, STARTING_CAPITAL,
    ADX_TREND_THRESHOLD, ADX_NOTREND_THRESHOLD,
)

def main():
    print("=" * 70)
    print("ARM v2 PIPELINE DIAGNOSTIC")
    print("=" * 70)

    # Load candle data
    cb = CandleBuilder()
    if not cb.bootstrap():
        print("ERROR: No historical data found")
        return
    df_1h = cb.get_df('1h')
    df_4h = cb.get_df('4h')
    df_daily = cb.get_df('daily')
    price = float(df_1h['close'].iloc[-1])
    print(f"\nData: {len(df_1h)} 1H candles, {len(df_4h)} 4H, {len(df_daily)} Daily")
    print(f"Current Price: ${price:,.2f}")

    # External data
    print("\n" + "-" * 70)
    print("EXTERNAL DATA")
    print("-" * 70)
    fg = fetch_fear_greed()
    funding = fetch_funding_rate()
    breadth = fetch_market_breadth()
    print(f"  Fear & Greed:   {fg} ({'Extreme Fear' if fg < 25 else 'Fear' if fg < 45 else 'Neutral' if fg < 55 else 'Greed'})")
    print(f"  Funding Rate:   {funding:.6f}")
    print(f"  Market Breadth: {breadth:.2%} coins rising")

    # L1: Regime
    print("\n" + "-" * 70)
    print("LAYER 1: REGIME DETECTION")
    print("-" * 70)
    adx = calculate_adx(df_1h)
    bb_width = calculate_bb_width(df_1h)
    atr_series = calculate_atr(df_1h)
    current_atr = float(atr_series.iloc[-1])
    atr_pct = float((atr_series.dropna() < current_atr).mean()) if len(atr_series.dropna()) > 50 else 0.5

    regime = detect_regime(df_1h, fg, funding, breadth)
    print(f"  ADX:            {adx:.1f} (>{ADX_TREND_THRESHOLD}=trending, <{ADX_NOTREND_THRESHOLD}=sideways)")
    print(f"  ATR:            ${current_atr:.2f}")
    print(f"  ATR percentile: {atr_pct:.2f} (>0.85=VOLATILE)")
    print(f"  BB Width:       {bb_width:.4f}")
    print(f"  >>> REGIME:     {regime}")

    blocked_at = None

    if regime == 'VOLATILE':
        blocked_at = f"L1: Regime is VOLATILE (ATR_pct={atr_pct:.2f} > 0.85)"
        print(f"  *** BLOCKED: {blocked_at}")

    # L2: Signal
    print("\n" + "-" * 70)
    print("LAYER 2: SIGNAL GENERATION")
    print("-" * 70)
    signal = generate_signal(df_1h, regime)
    direction = signal['direction']
    source = signal['source']

    close = df_1h['close']
    # RSI
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-10)
    rsi = float((100 - (100 / (1 + rs))).iloc[-1])

    # Z-score
    mean20 = float(close.rolling(20).mean().iloc[-1])
    std20 = float(close.rolling(20).std().iloc[-1])
    zscore = (price - mean20) / std20 if std20 > 0 else 0
    lower_bb = mean20 - 2 * std20
    upper_bb = mean20 + 2 * std20

    # Donchian
    don_high = float(close.rolling(20).max().iloc[-2])
    don_low = float(close.rolling(10).min().iloc[-2])

    print(f"  RSI(14):        {rsi:.1f} (buy<{RSI_OVERSOLD}, sell>{RSI_OVERBOUGHT})")
    print(f"  Z-score:        {zscore:.2f} (buy<-1.5, sell>1.5)")
    print(f"  Lower BB:       ${lower_bb:,.0f} (distance: {(price-lower_bb)/price*100:.2f}%)")
    print(f"  Upper BB:       ${upper_bb:,.0f}")
    print(f"  Donchian High:  ${don_high:,.0f} (breakout if price >= this)")
    print(f"  Donchian Low:   ${don_low:,.0f} (exit if price <= this)")
    print(f"  >>> SIGNAL:     {direction} ({source})")

    if not blocked_at and direction == 'HOLD':
        if regime == 'SIDEWAYS':
            rsi_gap = rsi - RSI_OVERSOLD
            blocked_at = f"L2: RSI={rsi:.1f} needs to drop {rsi_gap:.1f} more to hit {RSI_OVERSOLD} threshold"
        elif regime == 'TRENDING':
            blocked_at = f"L2: No Donchian breakout or EMA alignment"
        else:
            blocked_at = f"L2: VOLATILE regime → automatic HOLD"
        print(f"  *** BLOCKED: {blocked_at}")

    if direction == 'HOLD':
        print(f"\n{'=' * 70}")
        print(f"SUMMARY: Pipeline blocked at {blocked_at}")
        print(f"{'=' * 70}")
        return

    # L4: Timeframe
    print("\n" + "-" * 70)
    print("LAYER 4: MULTI-TIMEFRAME FILTER")
    print("-" * 70)
    tf = check_timeframe(df_1h, df_4h, df_daily, regime=regime)
    min_score = 1 if regime == 'SIDEWAYS' else 2
    print(f"  1H score:       {tf['scores']['1h']} (+1=bull, 0=neutral, -1=bear)")
    print(f"  4H score:       {tf['scores']['4h']}")
    print(f"  Daily score:    {tf['scores']['daily']}")
    print(f"  Total:          {tf['score']} (need >={min_score} for {regime})")
    print(f"  Multiplier:     {tf['multiplier']}")
    print(f"  >>> L4:         {'PASS' if tf['pass'] else 'BLOCKED'}")

    if not blocked_at and not tf['pass']:
        blocked_at = f"L4: TF score {tf['score']} < {min_score} needed for {regime}"
        print(f"  *** BLOCKED: {blocked_at}")

    if not tf['pass']:
        print(f"\n{'=' * 70}")
        print(f"SUMMARY: Pipeline blocked at {blocked_at}")
        print(f"{'=' * 70}")
        return

    # L5: XGBoost
    print("\n" + "-" * 70)
    print("LAYER 5: XGBOOST CONFIRMATION")
    print("-" * 70)
    try:
        from live_predictor import get_xgboost_signal
        price_history = df_1h.to_dict('records')
        spread_proxy = 0.001
        xgb_decision, xgb_prob = get_xgboost_signal(
            price_history, breadth=breadth,
            spread_proxy=spread_proxy, threshold=XGBOOST_MIN_PROBABILITY
        )
        print(f"  XGB Probability: {xgb_prob:.3f}")
        print(f"  Threshold:       {XGBOOST_MIN_PROBABILITY}")
        print(f"  >>> L5:          {'PASS' if xgb_prob >= XGBOOST_MIN_PROBABILITY else 'BLOCKED'}")

        if not blocked_at and xgb_prob < XGBOOST_MIN_PROBABILITY:
            blocked_at = f"L5: XGB prob {xgb_prob:.3f} < {XGBOOST_MIN_PROBABILITY} threshold"
            print(f"  *** BLOCKED: {blocked_at}")
    except Exception as e:
        print(f"  XGBoost not available: {e}")
        xgb_prob = 1.0

    if blocked_at:
        print(f"\n{'=' * 70}")
        print(f"SUMMARY: Pipeline blocked at {blocked_at}")
        print(f"{'=' * 70}")
        return

    # L6: Position Sizing
    print("\n" + "-" * 70)
    print("LAYER 6: POSITION SIZING")
    print("-" * 70)
    capital = STARTING_CAPITAL
    size = compute_position_size(
        current_capital=capital,
        peak_capital=capital,
        trade_history=[],
        regime=regime,
        timeframe_score=tf['score'],
        signal_score=xgb_prob * 100,
        atr_usd=current_atr,
        btc_price=price,
        current_position_open=False,
        rolling_sharpe_3day=0.0,
        timeframe_4h_bullish=tf['scores'].get('4h') == 1,
    )
    tf_mult = _timeframe_multiplier(tf['score'])
    print(f"  Capital:         ${capital:,.0f}")
    print(f"  TF multiplier:   {tf_mult} (for score {tf['score']})")
    print(f"  Regime mult:     {'1.0' if regime=='TRENDING' else '0.5' if regime=='SIDEWAYS' else '0.1'}")
    print(f"  Position size:   ${size:,.0f} ({size/capital*100:.2f}% of capital)")
    print(f"  >>> L6:          {'PASS' if size > 0 else 'BLOCKED'}")

    if size <= 0:
        blocked_at = f"L6: Position size is $0 (TF_mult={tf_mult}, regime={regime})"
        print(f"  *** BLOCKED: {blocked_at}")

    # Summary
    print(f"\n{'=' * 70}")
    if blocked_at:
        print(f"SUMMARY: Pipeline blocked at {blocked_at}")
    else:
        print(f"SUMMARY: ALL LAYERS PASS — trade would execute")
        print(f"  Direction: {direction} ({source})")
        print(f"  Size: ${size:,.0f}")
        print(f"  Regime: {regime}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
