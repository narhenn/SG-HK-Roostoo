"""
State Persistence: Save/load bot state to survive crashes.
Owner: Narhen
"""

import json
import os
import logging
from datetime import datetime, timezone
from config import STATE_FILE, STARTING_CAPITAL

log = logging.getLogger(__name__)


def save_state(state: dict):
    """Save bot state to JSON file."""
    state['last_saved'] = datetime.now(timezone.utc).isoformat()
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2, default=str)
    except Exception:
        log.exception("Failed to save state")


def load_state() -> dict:
    """Load bot state from JSON file. Returns empty dict if no state exists."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        log.exception("Failed to load state")
        return {}


def default_state() -> dict:
    """Return fresh default state."""
    return {
        'positions': [],             # Current open positions
        'trade_history': [],         # Completed trades (for Kelly)
        'peak_equity': STARTING_CAPITAL,
        'current_equity': STARTING_CAPITAL,
        'halt_until': None,          # Timestamp when halt expires
        'cooldown_until': None,      # Timestamp when cooldown expires
        'blocker_until': None,       # Reversal blocker cooldown
        'cycle_count': 0,
        'start_time': datetime.now(timezone.utc).isoformat(),
    }
