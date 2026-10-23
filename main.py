import os
import time
import math
import threading
import warnings
from datetime import datetime, date
import requests
import pandas as pd
import numpy as np
import streamlit as st
import plotly.graph_objects as go

warnings.filterwarnings('ignore')

# ==========================================
# GLOBAL CONFIGURATION
# ==========================================
# Pruned B-ETH_USDT to eliminate toxic asset drawdown
PAIRS = ["B-BTC_USDT", "B-SOL_USDT", "B-XRP_USDT", "B-ADA_USDT"]
CAPITAL_INR = 100000.00
RISK_PER_TRADE_INR = 250.00
USDT_INR_RATE = 86.00 

MAKER_FEE = 0.00025  
TAKER_FEE = 0.00050  
SLIPPAGE_RATE = 0.0005 

# V2 Ledger Split for clean live forward-testing
AMTE_LEDGER = "amte_v2_ledger.csv"
TW_LEDGER = "tw_v2_ledger.csv"
TW_TUNED_LEDGER = "tw_tuned_v2_ledger.csv"

LEDGERS = {
    "AMTE": AMTE_LEDGER,
    "TW_ORIG": TW_LEDGER,
    "TW_TUNED": TW_TUNED_LEDGER
}

# ==========================================
# CORE API FETCH & UTILS
# ==========================================
def fetch_live_data(pair, interval, limit=120):
    url = f"https://public.coindcx.com/market_data/candles?pair={pair}&interval={interval}&limit={limit}"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()
        if isinstance(data, dict): return pd.DataFrame()
        df = pd.DataFrame(data)
        df = df.sort_values(by='time').reset_index(drop=True)
        df['datetime'] = pd.to_datetime(df['time'], unit='ms')
        df.set_index(pd.DatetimeIndex(df["datetime"]), inplace=True)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
        return df
    except: return pd.DataFrame()

def log_trade(filename, trade_data):
    df_new = pd.DataFrame([trade_data])
    if os.path.exists(filename):
        try:
            df_old = pd.read_csv(filename)
            df_combined = pd.concat([df_old, df_new], ignore_index=True)
            df_combined.to_csv(filename, index=False)
        except Exception:
            df_new.to_csv(filename, mode='a', header=False, index=False)
    else:
        df_new.to_csv(filename, index=False)

def calculate_ehma(series, length=16):
    half_len = max(1, length // 2)
    sqrt_len = max(1, int(round(math.sqrt(length))))
    ema_half = series.ewm(span=half_len, adjust=False).mean()
    ema_full = series.ewm(span=length, adjust=False).mean()
    diff = 2 * ema_half - ema_full
    return diff.ewm(span=sqrt_len, adjust=False).mean()

def calculate_pivots(df, left=33, right=21, quick_right=3):
    highs = df['high'].values
    lows = df['low'].values
    n = len(df)
    last_p_high, last_p_low, last_q_high, last_q_low = np.nan, np.nan, np.nan, np.nan
    for i in range(left, n - right):
        if all(highs[i] >= highs[i - left:i]) and all(highs[i] >= highs[i+1:i + right + 1]):
            last_p_high = highs[i]
        if all(lows[i] <= lows[i - left:i]) and all(lows[i] <= lows[i+1:i + right + 1]):
            last_p_low = lows[i]
    for i in range(left, n - quick_right):
        if all(highs[i] >= highs[i - left:i]) and all(highs[i] >= highs[i+1:i + quick_right + 1]):
            last_q_high = highs[i]
        if all(lows[i] <= lows[i - left:i]) and all(lows[i] <= lows[i+1:i + quick_right + 1]):
            last_q_low = lows[i]
    return last_p_high, last_p_low, last_q_high, last_q
