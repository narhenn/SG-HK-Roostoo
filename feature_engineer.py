import pandas as pd
import numpy as np
import ta
import joblib

def calculate_features(df):
    """
    Takes raw OHLCV dataframe
    Returns dataframe with all 10 features calculated
    """
    print("Calculating features...")
    
    # Make a copy so we don't mess up original data
    data = df.copy()
    
    # ================================================
    # FEATURE 1: Log Returns (1, 3, 5, 10 bars)
    # log(current price / price N bars ago)
    # Positive = price went up, Negative = price went down
    # ================================================
    data['log_return_1']  = np.log(data['close'] / data['close'].shift(1))
    data['log_return_3']  = np.log(data['close'] / data['close'].shift(3))
    data['log_return_5']  = np.log(data['close'] / data['close'].shift(5))
    data['log_return_10'] = np.log(data['close'] / data['close'].shift(10))
    print("Feature 1 done - Log Returns")

    # ================================================
    # FEATURE 2: ATR/Price Ratio (Volatility)
    # ATR = average price movement per candle
    # Divide by price to make it a percentage
    # ================================================
    atr_indicator = ta.volatility.AverageTrueRange(
        high=data['high'],
        low=data['low'],
        close=data['close'],
        window=14
    )
    data['atr'] = atr_indicator.average_true_range()
    data['atr_ratio'] = data['atr'] / data['close']
    print("Feature 2 done - ATR Ratio")

    # ================================================
    # FEATURE 3: RSI(14) + RSI Change over 3 bars
    # RSI = overbought/oversold indicator (0-100)
    # RSI change = is RSI rising or falling?
    # ================================================
    rsi_indicator = ta.momentum.RSIIndicator(
        close=data['close'],
        window=14
    )
    data['rsi'] = rsi_indicator.rsi()
    data['rsi_change'] = data['rsi'] - data['rsi'].shift(3)
    print("Feature 3 done - RSI")

    # ================================================
    # FEATURE 4: MACD Histogram
    # MACD line - Signal line
    # Positive and growing = strong bullish momentum
    # ================================================
    macd_indicator = ta.trend.MACD(
        close=data['close'],
        window_slow=26,
        window_fast=12,
        window_sign=9
    )
    data['macd_histogram'] = macd_indicator.macd_diff()
    print("Feature 4 done - MACD Histogram")

    # ================================================
    # FEATURE 5: Bollinger Band %B
    # Where is price within the bands?
    # 0 = at lower band, 0.5 = middle, 1 = upper band
    # ================================================
    bb_indicator = ta.volatility.BollingerBands(
        close=data['close'],
        window=20,
        window_dev=2
    )
    data['bb_upper'] = bb_indicator.bollinger_hband()
    data['bb_lower'] = bb_indicator.bollinger_lband()
    # %B formula = (price - lower) / (upper - lower)
    data['bb_percent_b'] = (data['close'] - data['bb_lower']) / (data['bb_upper'] - data['bb_lower'])
    print("Feature 5 done - Bollinger Band %B")

    # ================================================
    # FEATURE 6: Volume Ratio
    # Current volume / average volume over last 20 bars
    # 2.0 = twice normal volume = significant activity
    # ================================================
    data['volume_ma20'] = data['volume'].rolling(window=20).mean()
    data['volume_ratio'] = data['volume'] / data['volume_ma20']
    print("Feature 6 done - Volume Ratio")

    # ================================================
    # FEATURE 7: Cyclical Time Encoding
    # Convert hour (0-23) to sin/cos so model knows
    # hour 23 is close to hour 0 (not opposite ends)
    # ================================================
    data['hour'] = data['timestamp'].dt.hour
    data['hour_sin'] = np.sin(2 * np.pi * data['hour'] / 24)
    data['hour_cos'] = np.cos(2 * np.pi * data['hour'] / 24)
    print("Feature 7 done - Time Encoding")

    # ================================================
    # FEATURE 8: Rolling Autocorrelation (20 bars)
    # How much do current moves repeat past moves?
    # High = trending, Low = random noise
    # ================================================
    data['autocorr'] = data['log_return_1'].rolling(window=20).apply(
        lambda x: pd.Series(x).autocorr(lag=1), raw=False
    )
    print("Feature 8 done - Autocorrelation")

    # ================================================
    # FEATURES 9 & 10: Breadth + Spread Proxy
    # These come from Roostoo API live
    # For training we use neutral placeholder values
    # Real values injected when bot runs live
    # ================================================
    # NOTE: breadth and spread_proxy were constant placeholders during training
    # (0.5 and 0.001), meaning XGBoost learned zero signal from them.
    # We still include them so the feature column order matches the saved model,
    # but they add no predictive value. A retrain with real Roostoo data would fix this.
    data['breadth'] = 0.5
    data['spread_proxy'] = 0.001
    print("Feature 9 & 10 done - Breadth + Spread (placeholders — no predictive value)")

    # ================================================
    # DROP ROWS WITH NaN (empty values)
    # First ~200 rows will have NaN because indicators
    # need history to calculate (ATR needs 14 bars, etc)
    # ================================================
    data = data.dropna()
    data = data.reset_index(drop=True)

    print(f"Features calculated! Shape: {data.shape}")
    print(f"Columns: {list(data.columns)}")
    
    return data

# ================================================
# RUN IT
# ================================================
# Load the CSV we downloaded
print("Loading btc_data.csv...")
df = pd.read_csv('btc_data.csv')
df['timestamp'] = pd.to_datetime(df['timestamp'])
print(f"Loaded {len(df)} rows")

# Calculate all features
df_features = calculate_features(df)

# Save with features
df_features.to_csv('btc_features.csv', index=False)
print("Saved to btc_features.csv")
print(df_features.head())

