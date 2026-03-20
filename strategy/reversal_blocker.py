# SECTION 1 — IMPORTS AND STATE
import time      # For time-related operations like checking cooldowns
import collections  # For deque to maintain rolling spread history

# Module-level state for cooldown tracking
cooldown_until = 0  # Unix timestamp when cooldown expires (0 = no cooldown)
consecutive_blocks = 0  # Counter for consecutive blocks (for logging)

# Rolling history of spreads for calculating average (last 20 values)
spread_history = collections.deque(maxlen=20)

# SECTION 2 — MAIN FUNCTION: check_reversal_block(prices, volumes, spread, signal)
def check_reversal_block(prices, volumes, spread, signal):
    """
    Checks if a BUY/SELL signal should be blocked due to reversal patterns.
    Prevents chasing pumps by detecting extreme moves, spread widening, or abnormal volume.
    If blocked, enforces 2-minute cooldown before next trade attempt.
    
    Parameters:
      prices: list of recent close prices, most recent last (e.g. [79800, 79900, 80000])
      volumes: list of recent volumes, same length as prices, most recent last
      spread: float — current (MinAsk - MaxBid) / LastPrice from Roostoo ticker
      signal: string — "BUY" or "SELL" from Layer 2
      
    Returns dictionary with decision, reason, and check details.
    """
    global cooldown_until, consecutive_blocks  # Declare globals for modification
    
    try:
        current_time = time.time()  # Get current unix timestamp
        
        # STEP 1 — CHECK COOLDOWN FIRST
        if current_time < cooldown_until:
            # We are in cooldown, block immediately without running checks
            cooldown_remaining = int(cooldown_until - current_time)
            return {
                "decision": "BLOCK",
                "reason": f"In cooldown: {cooldown_remaining} seconds remaining",
                "check1_extreme_move": False,  # Not checked due to cooldown
                "check2_spread": False,
                "check3_volume": False,
                "cooldown_remaining": cooldown_remaining
            }
        
        # STEP 2 — CHECK 1: EXTREME RECENT MOVE
        # Skip during bootstrap: Binance→Roostoo price gap (~$3-4k) looks like a
        # fake spike and blocks all trades for ~20 hours until live candles flush it out
        check1 = False
        reason1 = ""
        from data.candle_builder import BOOTSTRAP_DOMINANT
        if not BOOTSTRAP_DOMINANT and len(prices) >= 4:
            price_change_pct = ((prices[-1] - prices[-4]) / prices[-4]) * 100
            if abs(price_change_pct) > 2.0:
                check1 = True
                reason1 = f"Extreme move: {price_change_pct:.2f}% in last 3 candles"
        
        # STEP 3 — CHECK 2: SPREAD WIDENING
        check2 = False
        reason2 = ""
        spread_history.append(spread)  # Add current spread to rolling history
        if len(spread_history) >= 5:  # Need minimum 5 readings for meaningful average
            avg_spread = sum(spread_history) / len(spread_history)
            if spread > 1.5 * avg_spread:
                check2 = True
                reason2 = f"Spread widening: {spread:.6f} vs avg {avg_spread:.6f}"
        # If len(spread_history) < 5, check2 remains False (skip check)
        
        # STEP 4 — CHECK 3: ABNORMAL VOLUME
        # Skip during bootstrap: Binance vs Roostoo volumes are on totally different
        # scales, so any comparison across the boundary is meaningless
        check3 = False
        reason3 = ""
        if not BOOTSTRAP_DOMINANT and len(volumes) >= 5:
            avg_volume = sum(volumes[:-1]) / len(volumes[:-1])  # Exclude current candle
            current_volume = volumes[-1]
            if avg_volume > 0 and current_volume > 3.0 * avg_volume:
                check3 = True
                reason3 = f"Abnormal volume: {current_volume:.0f} vs avg {avg_volume:.0f}"
        
        # STEP 5 — COMBINE RESULTS
        if check1 or check2 or check3:
            # At least one check triggered — BLOCK the trade
            cooldown_until = current_time + 120  # 2 minutes = 2 cycles
            consecutive_blocks += 1
            
            # Combine reasons from all triggered checks
            reasons = []
            if check1: reasons.append(reason1)
            if check2: reasons.append(reason2)
            if check3: reasons.append(reason3)
            combined_reason = "; ".join(reasons)
            
            print(f"[L3] BLOCKED — {combined_reason}. Cooldown: 120s. Consecutive blocks: {consecutive_blocks}")
            return {
                "decision": "BLOCK",
                "reason": combined_reason,
                "check1_extreme_move": check1,
                "check2_spread": check2,
                "check3_volume": check3,
                "cooldown_remaining": 120
            }
        else:
            # All checks passed — ALLOW the trade
            consecutive_blocks = 0  # Reset counter on successful pass
            
            print("[L3] PASSED — no reversal signals detected")
            return {
                "decision": "PASS",
                "reason": "All checks passed",
                "check1_extreme_move": False,
                "check2_spread": False,
                "check3_volume": False,
                "cooldown_remaining": 0
            }
            
    except Exception as e:
        # Unexpected error — return PASS as safe fallback to avoid crashing bot
        print(f"[L3] ERROR in check_reversal_block: {e} — returning PASS as fallback")
        return {
            "decision": "PASS",
            "reason": f"Error occurred: {e} — fallback to PASS",
            "check1_extreme_move": False,
            "check2_spread": False,
            "check3_volume": False,
            "cooldown_remaining": 0
        }

# SECTION 3 — HELPER FUNCTION: is_in_cooldown()
def is_in_cooldown():
    """
    Quick check if we are currently in cooldown period.
    Returns True if blocked, False if available for trading.
    """
    return time.time() < cooldown_until

# SECTION 4 — HELPER FUNCTION: reset_cooldown()
def reset_cooldown():
    """
    Emergency reset of cooldown state.
    Sets cooldown_until to 0 and resets consecutive_blocks counter.
    Used for testing and manual overrides.
    """
    global cooldown_until, consecutive_blocks
    cooldown_until = 0
    consecutive_blocks = 0
    print("[L3] Cooldown reset manually")

# SECTION 5 — TEST BLOCK
if __name__ == "__main__":
    print("=== REVERSAL BLOCKER TEST ===")
    
    # TEST 1 — Normal market (should PASS)
    print("\nTest 1: Normal market")
    prices = [79800, 79850, 79900, 79950, 80000]  # Tiny moves, calm market
    volumes = [100, 105, 98, 102, 101]  # Consistent volume
    spread = 0.0001  # Normal spread
    result = check_reversal_block(prices, volumes, spread, "BUY")
    print(f"Result: {result}")
    assert result["decision"] == "PASS", "Test 1 should PASS"
    
    # TEST 2 — Extreme price move (should BLOCK on check 1)
    print("\nTest 2: Extreme price move")
    prices = [79000, 79200, 80500, 81000, 81500]  # 2.5%+ move in 3 candles
    volumes = [100, 105, 98, 102, 101]  # Normal volume
    spread = 0.0001  # Normal spread
    result = check_reversal_block(prices, volumes, spread, "BUY")
    print(f"Result: {result}")
    assert result["decision"] == "BLOCK", "Test 2 should BLOCK"
    assert result["check1_extreme_move"] == True, "Test 2 should trigger check1"
    
    # TEST 3 — Cooldown active (should BLOCK immediately without running checks)
    print("\nTest 3: Cooldown active")
    result = check_reversal_block(prices, volumes, spread, "BUY")  # Same params as Test 2
    print(f"Result: {result}")
    assert result["decision"] == "BLOCK", "Test 3 should BLOCK due to cooldown"
    print(f"Cooldown remaining: {result['cooldown_remaining']} seconds")
    
    # TEST 4 — Reset and test abnormal volume (should BLOCK on check 3)
    print("\nTest 4: Abnormal volume after reset")
    reset_cooldown()
    prices = [79800, 79850, 79900, 79950, 80000]  # Normal prices
    volumes = [100, 105, 98, 102, 850]  # Last volume is 8x average — extreme spike
    spread = 0.0001
    result = check_reversal_block(prices, volumes, spread, "BUY")
    print(f"Result: {result}")
    assert result["decision"] == "BLOCK", "Test 4 should BLOCK"
    assert result["check3_volume"] == True, "Test 4 should trigger check3"
    
    # TEST 5 — SELL signal while in PASS state (should also work)
    print("\nTest 5: SELL signal in normal conditions")
    reset_cooldown()
    prices = [80100, 80050, 80000, 79980, 79950]  # Slight downtrend, normal
    volumes = [100, 105, 98, 102, 101]
    spread = 0.0001
    result = check_reversal_block(prices, volumes, spread, "SELL")
    print(f"Result: {result}")
    
    print("\n=== ALL TESTS COMPLETE ===")
