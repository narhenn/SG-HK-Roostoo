"""
Layer 6: Position Sizing + Risk Management
Owner: Kireeti

Implements a multi-constraint sizing pipeline to protect Sortino/Sharpe/Calmar
by constraining risk at each step (Kelly, regime, timeframe, drawdown, hard caps).
"""

import os
import sys
import math
import statistics
from datetime import datetime, timedelta, timezone

# Allow running this module directly without installing the package.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config import (
    KELLY_FRACTION,
    MAX_POSITION_PCT,
    MAX_LOSS_PER_TRADE,
    RISK_PER_TRADE,
    DRAWDOWN_LEVEL_1,
    DRAWDOWN_LEVEL_2,
    DRAWDOWN_LEVEL_3,
    DRAWDOWN_LEVEL_4,
    DRAWDOWN_KILL,
    SHARPE_KILL,
    HALT_HOURS,
    KILL_HALT_HOURS,
    PARTIAL_FILL_THRESHOLD,
)
from data.state import load_state, save_state, default_state


# --- FUNCTION: _ensure_state ---
# What it does: Makes sure we have a usable state dictionary.
# Why we need it: Without state (saved memory), we would forget open positions,
#                 peak value, and risk limits after a crash or restart.
# Inputs: state (a dictionary or None) meaning the current saved memory.
# Outputs: a valid dictionary with all expected state fields.
def _ensure_state(state: dict) -> dict:
    # If the caller already gave us a state dictionary, use it as-is.
    if state is not None:
        return state
    # Otherwise, try to load state from disk so we remember past trades.
    loaded = load_state()
    # If no saved state exists, fall back to default values.
    return loaded if loaded else default_state()


# --- FUNCTION: _save_state ---
# What it does: Saves the state dictionary to disk.
# Why we need it: If the program crashes, we can restart and continue safely.
# Inputs: state (the dictionary to save), save_state_fn (optional save helper).
# Outputs: None (it writes to disk).
def _save_state(state: dict, save_state_fn=None):
    # Use the provided save function if given; otherwise use the default.
    fn = save_state_fn or save_state
    # Write the state to disk so we do not lose it on a crash.
    fn(state)


# --- FUNCTION: _update_peak_equity ---
# What it does: Updates the highest portfolio value we have ever seen.
# Why we need it: Drawdown (drop from peak) is measured from this value.
# Inputs: state (the saved memory), current_capital (current money value).
# Outputs: None (it updates the state in-place).
def _update_peak_equity(state: dict, current_capital: float):
    # Read the saved peak value; if missing, use current capital as peak.
    peak = state.get("peak_equity") or current_capital
    # If our portfolio is higher now, we have a new peak and should update it.
    if current_capital > peak:
        peak = current_capital
    # Save the updated peak and current equity into the state dictionary.
    state["peak_equity"] = peak
    state["current_equity"] = current_capital


# --- FUNCTION: _quarter_kelly_size ---
# What it does: Computes a safe betting fraction using quarter-Kelly.
# Why we need it: Full Kelly can cause huge drawdowns; quarter-Kelly is safer.
# Inputs: trade_history (recent trade outcomes), current_capital (money now).
# Outputs: (fraction, size_usd) where fraction is % of money to bet.
#
# Note: "Kelly" is a formula that tries to maximize long-term growth, but it
#       can be too aggressive. We use 0.25 (25%) of Kelly to reduce risk.
def _quarter_kelly_size(trade_history: list, current_capital: float) -> tuple:
    # Keep only the most recent 20 trades (a "rolling window" [moving list]).
    # Why 20? It is a balance: not too small (noisy) and not too big (stale).
    recent = (trade_history or [])[-20:]

    # If we have fewer than 20 trades, we do a safe "cold start" default.
    # These numbers (0.55 win rate and 1.3 reward/risk) come from the spec.
    # Example: if we win 55% and win 1.3x what we lose, Kelly = 0.269.
    if len(recent) < 20:
        # 0.269 means 26.9% full Kelly; we will later take only 25% of that.
        kelly = 0.269
    else:
        # Pull out only the winning trades (positive pnl_pct).
        wins = [t.get("pnl_pct") for t in recent if t and t.get("pnl_pct", 0) > 0]
        # Pull out only the losing trades (negative pnl_pct) and flip sign.
        losses = [-t.get("pnl_pct") for t in recent if t and t.get("pnl_pct", 0) < 0]
        # If all trades are wins or all are losses, we lack balance data.
        if not wins or not losses:
            # Fall back to safe cold-start defaults so we do not overfit.
            kelly = 0.269
        else:
            # win_rate = wins / total trades. Example: 12 wins / 20 = 0.60.
            win_rate = len(wins) / len(recent)
            # avg_win = average % gain on winning trades.
            avg_win = statistics.mean(wins)
            # avg_loss = average % loss on losing trades (positive value).
            avg_loss = statistics.mean(losses)
            # If avg_loss is zero or negative, avoid division errors.
            if avg_loss <= 0:
                return 0.0, 0.0
            # reward_risk = avg_win / avg_loss. Example: 0.015 / 0.01 = 1.5.
            reward_risk = avg_win / avg_loss
            # Kelly formula explanation:
            #   kelly = (win_rate * reward_risk - (1 - win_rate)) / reward_risk
            #   win_rate = how often we win (e.g., 0.60)
            #   reward_risk = how big wins are vs losses (e.g., 1.5)
            # Example: (0.60*1.5 - 0.40) / 1.5 = (0.90 - 0.40) / 1.5 = 0.333
            # Result means: "Bet 33.3%" if using full Kelly (too risky).
            kelly = (win_rate * reward_risk - (1 - win_rate)) / reward_risk
    # If Kelly is negative, it means the strategy is losing. Do not trade.
    if kelly <= 0:
        return 0.0, 0.0

    # Quarter-Kelly: multiply by 0.25 to reduce drawdown risk.
    # 0.25 means we take only one quarter of the aggressive Kelly size.
    quarter_kelly = kelly * KELLY_FRACTION
    # Convert fraction to a dollar size: fraction * current capital.
    return quarter_kelly, quarter_kelly * current_capital


# --- FUNCTION: _regime_multiplier ---
# What it does: Adjusts size based on market regime.
# Why we need it: Different market conditions are more or less reliable.
# Inputs: regime ("TRENDING", "SIDEWAYS", "VOLATILE").
# Outputs: a multiplier (1.0, 0.5, or 0.1).
#
# Numbers:
# - 1.0 means full size in TRENDING (trend is more reliable).
# - 0.5 means half size in SIDEWAYS (noisy, choppy moves).
# - 0.1 means tiny size in VOLATILE (danger zone; protect capital).
def _regime_multiplier(regime: str) -> float:
    # Return the multiplier based on the regime name.
    return {"TRENDING": 1.0, "SIDEWAYS": 0.5, "VOLATILE": 0.1}.get(regime, 0.1)


# --- FUNCTION: _timeframe_multiplier ---
# What it does: Scales size based on how many timeframes agree.
# Why we need it: When multiple timeframes agree, signals are more reliable.
# Inputs: timeframe_score (3 means all agree, 2 means partial agreement).
# Outputs: a multiplier (1.0 for full confidence, 0.5 for half confidence).
#
# Numbers:
# - 3 -> 1.0 (all 1H, 4H, Daily agree).
# - 2 -> 0.5 (only two agree; be cautious).
def _timeframe_multiplier(timeframe_score: int) -> float:
    # Score 1 is valid for SIDEWAYS mean-reversion — don't block it
    return {3: 1.0, 2: 0.5, 1: 0.25}.get(timeframe_score, 0.0)


# --- FUNCTION: _is_halted ---
# What it does: Checks whether trading is paused until a future time.
# Why we need it: After big losses, we pause to prevent more damage.
# Inputs: state (saved memory with halt_until timestamp).
# Outputs: True if halted, False if trading is allowed.
def _is_halted(state: dict) -> bool:
    # Read the saved halt time (if any).
    halt_until = state.get("halt_until")
    # If there is no halt time, we are not halted.
    if not halt_until:
        return False
    try:
        # Compare current time to halt time.
        return datetime.now(timezone.utc) < datetime.fromisoformat(halt_until)
    except ValueError:
        # If the stored time is malformed, treat as not halted.
        return False


# --- FUNCTION: _set_halt ---
# What it does: Sets the halt timer into the future.
# Why we need it: A timed pause protects the portfolio after big losses.
# Inputs: state (saved memory), hours (how long to pause).
# Outputs: None (updates the state in-place).
def _set_halt(state: dict, hours: int):
    # Example: hours=4 means pause until now + 4 hours.
    state["halt_until"] = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


# --- FUNCTION: _drawdown_cap ---
# What it does: Applies drawdown-based caps and halt rules.
# Why we need it: Drawdown is the most dangerous time; we reduce risk or stop.
# Inputs:
#   peak_capital (highest value we ever had),
#   current_capital (money now),
#   signal_score (0-100 confidence score),
#   timeframe_score (how many timeframes agree),
#   timeframe_4h_bullish (True if 4H is bullish),
#   state (saved memory for halts).
# Outputs: (cap_usd, allowed) where cap_usd is max size; allowed indicates trade.
#
# Drawdown example:
#   peak = 1,000,000; current = 960,000
#   drawdown = (1,000,000 - 960,000) / 1,000,000 = 0.04 (4%)
#   At 4% we are in the 2%-5% band and must be cautious.
def _drawdown_cap(
    peak_capital: float,
    current_capital: float,
    signal_score: float,
    timeframe_score: int,
    timeframe_4h_bullish: bool,
    state: dict,
) -> tuple:
    # If peak is zero (should not happen), allow trading with no cap.
    if peak_capital <= 0:
        return math.inf, True

    # Drawdown formula:
    #   drawdown_pct = (peak - current) / peak
    # Example: (1,000,000 - 975,000) / 1,000,000 = 0.025 (2.5%)
    drawdown_pct = (peak_capital - current_capital) / peak_capital

    # Reset halt flag if drawdown recovered
    if drawdown_pct < DRAWDOWN_LEVEL_3:
        state.pop("_dd_8pct_halted", None)

    # 1) Drawdown < 2% (DRAWDOWN_LEVEL_1 = 0.02)
    # We are healthy; no cap.
    if drawdown_pct < DRAWDOWN_LEVEL_1:
        return math.inf, True

    # 2) 2% to <5% drawdown
    # We cap size at 25% and require signal_score >= 70.
    if DRAWDOWN_LEVEL_1 <= drawdown_pct < DRAWDOWN_LEVEL_2:
        # If signal score is below 70, skip trading (not strong enough).
        if signal_score < 70:
            return 0.0, False
        # Cap = 0.25 * current capital. 0.25 means 25% max size.
        return 0.25 * current_capital, True

    # 3) 5% to <8% drawdown
    # We cap size at 15% and require signal_score >= 80.
    if DRAWDOWN_LEVEL_2 <= drawdown_pct < DRAWDOWN_LEVEL_3:
        if signal_score < 80:
            return 0.0, False
        # Cap = 0.15 * current capital. 0.15 means 15% max size.
        return 0.15 * current_capital, True

    # 4) 8% to <10% drawdown
    # First time: halt for 4 hours. After halt expires: allow at 10% cap.
    # Track whether we already halted for this drawdown level.
    if DRAWDOWN_LEVEL_3 <= drawdown_pct < DRAWDOWN_LEVEL_4:
        if not state.get("_dd_8pct_halted"):
            # First time hitting 8% — halt for 4 hours
            _set_halt(state, HALT_HOURS)
            state["_dd_8pct_halted"] = True
            return 0.0, False
        # Already halted once for this level — allow at 10% cap
        return 0.10 * current_capital, True

    # 5) Drawdown >= 10%
    # Emergency mode: cap at 5%, only if 4H timeframe is bullish.
    if drawdown_pct >= DRAWDOWN_LEVEL_4:
        if not timeframe_4h_bullish:
            return 0.0, False
        # Cap = 0.05 * current capital (5% max size).
        return 0.05 * current_capital, True

    # Default safe fallback: no cap and allowed.
    return math.inf, True


# --- FUNCTION: _hard_limits ---
# What it does: Applies absolute caps that can never be exceeded.
# Why we need it: Even if other logic says "go big", we enforce safety fences.
# Inputs: current_capital (money now), atr_usd (volatility), btc_price.
# Outputs: a dictionary of cap values in USD.
#
# Explanation of numbers:
# - MAX_POSITION_PCT = 0.35 means never put more than 35% of total money
#   into one trade. 35% is big but avoids catastrophic all-in bets.
# - MAX_LOSS_PER_TRADE = 0.015 means max loss is 1.5% of total money.
# - RISK_PER_TRADE = 0.005 means we prefer risking only 0.5% per trade.
# - ATR_STOP_MULTIPLIER is used elsewhere as 1.5x ATR stop distance.
#
# ATR (Average True Range [average movement]) is how much price usually moves.
# If ATR is $500, then 1.5x ATR means $750 stop distance.
# If we are willing to lose $5,000 and stop distance is $750, we can buy
# $5,000 / $750 = 6.666 BTC units worth of risk, then multiply by price.
def _hard_limits(current_capital: float, atr_usd: float, btc_price: float) -> dict:
    # Hard cap #1: 35% of current capital. Example: 0.35 * 1,000,000 = 350,000.
    hard_cap_35pct = MAX_POSITION_PCT * current_capital
    # Hard cap #2: max loss in USD (1.5% of capital).
    max_loss_usd = MAX_LOSS_PER_TRADE * current_capital
    # Hard cap #3: risk budget per trade (0.5% of capital).
    risk_cap_usd = RISK_PER_TRADE * current_capital

    # Stop distance = 1.5 * ATR. If ATR is 500, stop distance is 750.
    # We use max with 1e-8 to avoid divide-by-zero errors.
    stop_distance = max(atr_usd * 1.5, 1e-8)
    # Convert risk budget into BTC quantity: risk / stop_distance.
    volatility_size_btc = risk_cap_usd / stop_distance
    # Convert BTC quantity into USD size: quantity * price.
    volatility_size_usd = volatility_size_btc * btc_price

    # Max loss size based on 1.5% loss cap:
    # Example: max_loss_usd = 15,000, stop_distance = 750, price = 80,000
    # max_loss_size_usd = 15,000 * 80,000 / 750 = 1,600,000 (still capped later).
    max_loss_size_usd = max_loss_usd * btc_price / stop_distance

    # Return all caps so the caller can take the smallest one.
    return {
        "hard_cap_35pct": hard_cap_35pct,
        "risk_cap_usd": risk_cap_usd,
        "volatility_size_usd": volatility_size_usd,
        "max_loss_size_usd": max_loss_size_usd,
    }


# --- FUNCTION: handle_partial_fill ---
# What it does: Decides what to do when an order only fills partially.
# Why we need it: A weak fill means the market did not want to sell to us,
#                 which is a weak signal and we should back off.
# Inputs:
#   ordered_qty (the BTC we wanted to buy),
#   filled_qty (how much BTC we actually got),
#   state (saved memory),
#   market_sell_fn (function to sell back if needed),
#   cooldown_seconds (wait time after rejection),
#   save_state_fn (custom save helper).
# Outputs: dict with accepted/rejected and fill percentage.
#
# Partial fill explanation:
#   If we asked for 1.0 BTC and got 0.6 BTC, filled_pct = 0.6 (60%).
#   >50% means keep it (signal is strong enough).
#   <=50% means sell it back and cool down (weak signal).
def handle_partial_fill(
    ordered_qty: float,
    filled_qty: float,
    state: dict = None,
    market_sell_fn=None,
    cooldown_seconds: int = 60,
    save_state_fn=None,
) -> dict:
    # Ensure we have a state dictionary to update.
    state = _ensure_state(state)
    # If ordered quantity is zero or negative, we cannot make a decision.
    if ordered_qty <= 0:
        return {"accepted": False, "reason": "invalid_order_qty"}

    # Compute how much of the order was filled.
    # Example: 0.6 / 1.0 = 0.6 (60%).
    filled_pct = filled_qty / ordered_qty
    # If more than 50% is filled, we accept and manage it as a real position.
    if filled_pct > PARTIAL_FILL_THRESHOLD:
        return {
            "accepted": True,
            "reason": "partial_fill_accepted",
            "filled_pct": filled_pct,
        }

    # If 50% or less is filled, we consider it weak and sell back immediately.
    if market_sell_fn is not None and filled_qty > 0:
        # We ask the exchange to sell the filled amount at market price.
        market_sell_fn(filled_qty)

    # Set a short cooldown so we do not jump right back in.
    state["cooldown_until"] = (datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)).isoformat()
    # Save state so cooldown is remembered if we crash.
    _save_state(state, save_state_fn)
    return {
        "accepted": False,
        "reason": "partial_fill_rejected",
        "filled_pct": filled_pct,
    }


# --- FUNCTION: compute_position_size ---
# What it does: The main Layer 6 function that returns how many dollars to use.
# Why we need it: This is the safety gate that prevents catastrophic drawdowns.
# Inputs (plain English):
#   current_capital: how much money we have right now.
#   peak_capital: the highest money value we ever had.
#   trade_history: last trades with gains/losses (for Kelly).
#   regime: market condition (TRENDING/SIDEWAYS/VOLATILE).
#   timeframe_score: how many charts agree (2 or 3).
#   signal_score: model confidence (0-100).
#   atr_usd: typical price movement in dollars (ATR).
#   btc_price: current BTC price.
#   current_position_open: True if we already hold BTC.
#   rolling_sharpe_3day: recent performance quality.
#   timeframe_4h_bullish: True if 4H chart is up.
#   state: saved memory dictionary.
# Outputs: final_position_size_usd (a dollar number, or 0 if blocked).
def compute_position_size(
    current_capital: float,
    peak_capital: float,
    trade_history: list,
    regime: str,
    timeframe_score: int,
    signal_score: float,
    atr_usd: float,
    btc_price: float,
    current_position_open: bool,
    rolling_sharpe_3day: float,
    timeframe_4h_bullish: bool = False,
    state: dict = None,
    save_state_fn=None,
    close_all_positions_fn=None,
) -> float:
    # Load or create the state dictionary so we can track risk limits.
    state = _ensure_state(state)
    # Update peak equity so drawdown calculations are correct.
    _update_peak_equity(state, current_capital)

    # --- EMERGENCY KILL SWITCH ---
    # We stop everything if drawdown > 15% or Sharpe < -0.5.
    # 15% drawdown example: 1,000,000 -> 850,000 (too deep for Calmar).
    if peak_capital > 0:
        drawdown_pct = (peak_capital - current_capital) / peak_capital
    else:
        drawdown_pct = 0.0
    # If either condition is triggered, we close positions and halt for 24 hours.
    if drawdown_pct > DRAWDOWN_KILL or rolling_sharpe_3day < SHARPE_KILL:
        if not state.get('_kill_switch_fired'):
            state['_kill_switch_fired'] = True
            if close_all_positions_fn is not None:
                close_all_positions_fn()
            _set_halt(state, KILL_HALT_HOURS)
            _save_state(state, save_state_fn)
            try:
                from execution.alerts import alert_kill_switch
                alert_kill_switch(drawdown_pct, rolling_sharpe_3day, current_capital)
            except Exception:
                pass
        return 0.0

    # If the bot is currently halted, do not trade.
    if _is_halted(state):
        _save_state(state, save_state_fn)
        return 0.0

    # If we already have a position, do not open another one.
    if current_position_open:
        _save_state(state, save_state_fn)
        return 0.0

    # --- STEP A: QUARTER-KELLY BASE SIZE ---
    quarter_kelly_fraction, quarter_kelly_size = _quarter_kelly_size(trade_history, current_capital)
    # If Kelly says "do not trade", return 0.
    if quarter_kelly_fraction <= 0:
        _save_state(state, save_state_fn)
        return 0.0

    # --- STEP B: REGIME MULTIPLIER ---
    # Example: TRENDING -> 1.0, SIDEWAYS -> 0.5, VOLATILE -> 0.1.
    regime_size = quarter_kelly_size * _regime_multiplier(regime)

    # --- STEP C: TIMEFRAME MULTIPLIER ---
    # Example: score 3 -> 1.0, score 2 -> 0.5.
    timeframe_size = regime_size * _timeframe_multiplier(timeframe_score)
    # If timeframe multiplier yields zero, skip trade.
    if timeframe_size <= 0:
        _save_state(state, save_state_fn)
        return 0.0

    # --- STEP D: DRAWDOWN THROTTLE ---
    # Drawdown caps reduce size or block trades in losing periods.
    drawdown_cap, allowed = _drawdown_cap(
        peak_capital,
        current_capital,
        signal_score,
        timeframe_score,
        timeframe_4h_bullish,
        state,
    )
    # If drawdown logic says "do not trade", return 0.
    if not allowed:
        _save_state(state, save_state_fn)
        return 0.0

    # --- STEP E: HARD LIMITS ---
    # These caps are absolute and never exceeded.
    hard = _hard_limits(current_capital, atr_usd, btc_price)
    hard_cap_35pct = hard["hard_cap_35pct"]
    volatility_size_usd = hard["volatility_size_usd"]
    max_loss_size_usd = hard["max_loss_size_usd"]

    # --- FINAL SIZE ---
    # We take the smallest of all safety caps to protect capital.
    final_size = min(
        timeframe_size,
        drawdown_cap,
        hard_cap_35pct,
        volatility_size_usd,
        max_loss_size_usd,
    )

    # If final size is negative for any reason, clamp to zero.
    final_size = max(final_size, 0.0)
    # Save state after sizing decision for crash safety.
    _save_state(state, save_state_fn)
    return final_size
