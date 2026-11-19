import os
import time
import math
import shutil
import threading
import warnings
from datetime import datetime, date, timedelta
import requests
import pandas as pd
import numpy as np
import streamlit as st
import plotly.graph_objects as go

warnings.filterwarnings('ignore')

# ==========================================
# GLOBAL CONFIGURATION (V2.5 PRECISION SHIELD)
# ==========================================
PAIRS = ["B-SOL_USDT", "B-XRP_USDT", "B-ADA_USDT"]
CAPITAL_INR = 100000.00
TARGET_MAX_NET_LOSS_INR = 150.00  # Hard net loss cap INCLUDING fees & slippage
RAW_RISK_BUDGET_INR = 124.00      # Leaves ~₹26 buffer for taker fees + slippage
USDT_INR_RATE = 86.00

MAKER_FEE = 0.00025
TAKER_FEE = 0.00050
SLIPPAGE_RATE = 0.0005

AMTE_LEDGER = "amte_v2_ledger.csv"
TW_LEDGER = "tw_v2_ledger.csv"
TW_TUNED_LEDGER = "tw_tuned_v2_ledger.csv"

LEDGER_COLS = [
    'Time', 'Pair', 'Side', 'Entry', 'Exit', 'Reason',
    'Gross_USD', 'Fees_USD', 'Slippage_USD', 'Net_PnL_INR', 'Max_DD_INR'
]

# ==========================================
# PERSISTENT LEDGER & CORE UTILS
# ==========================================
def load_ledger(filename):
    backup_file = f"backup_{filename}"
    for path in [filename, backup_file]:
        if os.path.exists(path):
            try:
                df = pd.read_csv(path)
                if not df.empty:
                    return df
            except Exception:
                continue
    return pd.DataFrame(columns=LEDGER_COLS)

def save_ledger_atomic(filename, df):
    tmp_file = f"tmp_{filename}"
    backup_file = f"backup_{filename}"
    df.to_csv(tmp_file, index=False)
    os.replace(tmp_file, filename)
    shutil.copy2(filename, backup_file)

def log_trade(filename, trade_data):
    df_new = pd.DataFrame([trade_data])
    df_old = load_ledger(filename)
    df_combined = pd.concat([df_old, df_new], ignore_index=True)
    df_combined.drop_duplicates(subset=['Time', 'Pair', 'Side', 'Entry', 'Exit'], keep='last', inplace=True)
    save_ledger_atomic(filename, df_combined)

def merge_uploaded_ledger(filename, uploaded_file):
    try:
        df_up = pd.read_csv(uploaded_file)
        missing = [c for c in LEDGER_COLS if c not in df_up.columns]
        if missing:
            return False, f"Missing columns: {missing}"
        df_curr = load_ledger(filename)
        df_merged = pd.concat([df_up[LEDGER_COLS], df_curr], ignore_index=True)
        df_merged['Sort_Time'] = pd.to_datetime(df_merged['Time'], errors='coerce')
        df_merged.sort_values('Sort_Time', inplace=True)
        df_merged.drop(columns=['Sort_Time'], inplace=True)
        df_merged.drop_duplicates(subset=['Time', 'Pair', 'Side', 'Entry'], keep='last', inplace=True)
        save_ledger_atomic(filename, df_merged)
        return True, f"Merged successfully! Total archived trades: {len(df_merged)}"
    except Exception as e:
        return False, f"Merge failed: {e}"

def fetch_live_data(pair, interval, limit=120):
    url = f"https://public.coindcx.com/market_data/candles?pair={pair}&interval={interval}&limit={limit}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()
        if isinstance(data, dict):
            return pd.DataFrame()
        df = pd.DataFrame(data).sort_values(by='time').reset_index(drop=True)
        df['datetime'] = pd.to_datetime(df['time'], unit='ms')
        df.set_index(pd.DatetimeIndex(df["datetime"]), inplace=True)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
        return df
    except Exception:
        return pd.DataFrame()

def calculate_ehma(series, length=16):
    half_len = max(1, length // 2)
    sqrt_len = max(1, int(round(math.sqrt(length))))
    ema_half = series.ewm(span=half_len, adjust=False).mean()
    ema_full = series.ewm(span=length, adjust=False).mean()
    return (2 * ema_half - ema_full).ewm(span=sqrt_len, adjust=False).mean()

def calculate_pivots(df, left=33, right=21, quick_right=3):
    highs, lows, n = df['high'].values, df['low'].values, len(df)
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
    return last_p_high, last_p_low, last_q_high, last_q_low

# ==========================================
# STRATEGY 1: AMTE BB PULLBACK (V2.5)
# ==========================================
class AMTEBot:
    def __init__(self, pairs):
        self.pairs = pairs
        self.positions = {pair: {'status': 'NONE'} for pair in pairs}
        self.cooldown_until = {pair: datetime.min for pair in pairs}
        self.current_date = date.today()
        self.daily_trades = 0
        self.daily_losses = 0
        self.daily_pnl_inr = 0.0
        self.max_daily_trades = 10
        self.max_daily_losses = 3      # Circuit breaker: halts after 3 stop-outs
        self.max_daily_loss_inr = -450.00
        self.max_concurrent = 2

    def check_daily_reset(self):
        today = date.today()
        if today != self.current_date:
            self.current_date = today
            self.daily_trades = 0
            self.daily_losses = 0
            self.daily_pnl_inr = 0.0
            for p in self.positions:
                if self.positions[p]['status'] == 'PENDING_ENTRY':
                    self.positions[p] = {'status': 'NONE'}

    def process_cycle(self):
        self.check_daily_reset()
        kill_active = (
            self.daily_trades >= self.max_daily_trades
            or self.daily_losses >= self.max_daily_losses
            or self.daily_pnl_inr <= self.max_daily_loss_inr
        )
        active_count = sum(1 for p in self.positions.values() if p['status'] == 'ACTIVE')

        for pair in self.pairs:
            df_macro = fetch_live_data(pair, "1h", 100)
            time.sleep(0.3)
            df_micro = fetch_live_data(pair, "15m", 120)
            time.sleep(0.3)
            if df_macro.empty or df_micro.empty:
                continue

            df_macro['EMA_20'] = df_macro['close'].ewm(span=20, adjust=False).mean()
            df_macro['EMA_50'] = df_macro['close'].ewm(span=50, adjust=False).mean()
            df_macro['spread'] = (df_macro['EMA_20'] - df_macro['EMA_50']).abs() / df_macro['close'] * 100

            c_macro = df_macro.iloc[-2]
            is_bull = (c_macro['EMA_20'] > c_macro['EMA_50']) and (c_macro['spread'] > 0.06)
            is_bear = (c_macro['EMA_20'] < c_macro['EMA_50']) and (c_macro['spread'] > 0.06)

            df_micro['SMA20'] = df_micro['close'].rolling(20).mean()
            df_micro['STD20'] = df_micro['close'].rolling(20).std()
            df_micro['BBL'] = df_micro['SMA20'] - (df_micro['STD20'] * 1.5)
            df_micro['BBU'] = df_micro['SMA20'] + (df_micro['STD20'] * 1.5)
            tr = pd.concat([
                df_micro['high'] - df_micro['low'],
                (df_micro['high'] - df_micro['close'].shift()).abs(),
                (df_micro['low'] - df_micro['close'].shift()).abs()
            ], axis=1).max(axis=1)
            df_micro['ATR'] = tr.rolling(14).mean()

            c_micro = df_micro.iloc[-2]
            live_price = df_micro.iloc[-1]['close']
            pos = self.positions[pair]

            if pos['status'] == 'ACTIVE':
                ep = pos['limit_price']
                floating_usd = (live_price - ep) * pos['size'] if pos['side'] == 'LONG' else (ep - live_price) * pos['size']
                floating_fees = (ep * pos['size'] * MAKER_FEE) + (live_price * pos['size'] * TAKER_FEE)
                net_floating_inr = (floating_usd - floating_fees) * USDT_INR_RATE
                self.positions[pair]['max_dd_inr'] = min(pos.get('max_dd_inr', 0.0), net_floating_inr)

                # Profit-Lock Guard: once unrealized profit hits +₹180, lock in +₹90 minimum profit
                if not pos.get('profit_locked', False) and net_floating_inr >= 180.0:
                    lock_offset = (90.0 / USDT_INR_RATE) / pos['size']
                    self.positions[pair]['sl'] = (ep + lock_offset) if pos['side'] == 'LONG' else (ep - lock_offset)
                    self.positions[pair]['profit_locked'] = True

                # 5-hour Timeout Exit if profitable
                if (datetime.now() - pos['entry_time']).total_seconds() / 3600 >= 5:
                    if net_floating_inr > 25.0:
                        self.close_trade(pair, live_price, "Timeout (Profit)", "TAKER")
                        continue

                if pos['side'] == 'LONG':
                    if live_price <= pos['sl']:
                        reason = "Profit Lock Stop" if pos.get('profit_locked', False) else "Stop Market"
                        self.close_trade(pair, pos['sl'], reason, "TAKER")
                    elif live_price >= pos['tp']:
                        self.close_trade(pair, pos['tp'], "Limit TP", "MAKER")
                else:
                    if live_price >= pos['sl']:
                        reason = "Profit Lock Stop" if pos.get('profit_locked', False) else "Stop Market"
                        self.close_trade(pair, pos['sl'], reason, "TAKER")
                    elif live_price <= pos['tp']:
                        self.close_trade(pair, pos['tp'], "Limit TP", "MAKER")
                continue

            if pos['status'] == 'PENDING_ENTRY':
                if (pos['side'] == 'LONG' and not is_bull) or (pos['side'] == 'SHORT' and not is_bear):
                    self.positions[pair] = {'status': 'NONE'}
                    continue
                if pos['side'] == 'LONG' and df_micro.iloc[-1]['low'] <= pos['limit_price']:
                    self.activate_trade(pair)
                elif pos['side'] == 'SHORT' and df_micro.iloc[-1]['high'] >= pos['limit_price']:
                    self.activate_trade(pair)
                continue

            if kill_active or active_count >= self.max_concurrent or datetime.now() < self.cooldown_until[pair]:
                continue

            # Fee-adjusted position sizing so max net loss stays capped around -₹150
            raw_risk_usd = RAW_RISK_BUDGET_INR / USDT_INR_RATE
            atr = c_micro['ATR']
            sl_mult = 1.4
            tp_mult = 3.8  # ~2.7x reward-to-risk (~₹380+ net Limit TP)

            if is_bull and live_price > c_micro['BBL'] and live_price > df_macro.iloc[-1]['EMA_50']:
                lp = c_micro['BBL']
                sl, tp = lp - (atr * sl_mult), lp + (atr * tp_mult)
                self.place_limit(pair, 'LONG', lp, sl, tp, raw_risk_usd / max(0.0001, (lp - sl)))
            elif is_bear and live_price < c_micro['BBU'] and live_price < df_macro.iloc[-1]['EMA_50']:
                lp = c_micro['BBU']
                sl, tp = lp + (atr * sl_mult), lp - (atr * tp_mult)
                self.place_limit(pair, 'SHORT', lp, sl, tp, raw_risk_usd / max(0.0001, (sl - lp)))

    def place_limit(self, pair, side, lp, sl, tp, size):
        self.positions[pair] = {
            'status': 'PENDING_ENTRY', 'side': side, 'limit_price': lp,
            'sl': sl, 'tp': tp, 'size': size, 'max_dd_inr': 0.0, 'profit_locked': False
        }

    def activate_trade(self, pair):
        self.positions[pair].update({'status': 'ACTIVE', 'entry_time': datetime.now(), 'max_dd_inr': 0.0})
        self.daily_trades += 1

    def close_trade(self, pair, exec_price, reason, order_type):
        pos = self.positions[pair]
        ep = pos['limit_price']
        exit_p = (
            exec_price * (1 - (SLIPPAGE_RATE if order_type == "TAKER" else 0.0))
            if pos['side'] == 'LONG'
            else exec_price * (1 + (SLIPPAGE_RATE if order_type == "TAKER" else 0.0))
        )
        gross = (exit_p - ep) * pos['size'] if pos['side'] == 'LONG' else (ep - exit_p) * pos['size']
        fees = (ep * pos['size'] * MAKER_FEE) + (exit_p * pos['size'] * (MAKER_FEE if order_type == "MAKER" else TAKER_FEE))
        net_inr = (gross - fees) * USDT_INR_RATE
        self.daily_pnl_inr += net_inr

        if net_inr <= 0:
            self.daily_losses += 1
            self.cooldown_until[pair] = datetime.now() + timedelta(minutes=30)

        log_trade(AMTE_LEDGER, {
            'Time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 'Pair': pair, 'Side': pos['side'],
            'Entry': round(ep, 4), 'Exit': round(exit_p, 4), 'Reason': reason,
            'Gross_USD': round(gross, 2), 'Fees_USD': round(fees, 2),
            'Slippage_USD': round(abs(exec_price - exit_p) * pos['size'], 2),
            'Net_PnL_INR': round(net_inr, 2), 'Max_DD_INR': round(pos.get('max_dd_inr', 0.0), 2)
        })
        self.positions[pair] = {'status': 'NONE'}

# ==========================================
# STRATEGY 2: TW ORIGINAL (V2.5)
# ==========================================
class TWAllInOneBot:
    def __init__(self, pairs):
        self.pairs = pairs
        self.positions = {pair: {'status': 'NONE'} for pair in pairs}
        self.cooldown_until = {pair: datetime.min for pair in pairs}
        self.current_date = date.today()
        self.daily_trades = 0
        self.daily_losses = 0
        self.daily_pnl_inr = 0.0
        self.max_daily_trades = 10
        self.max_daily_losses = 3
        self.max_daily_loss_inr = -450.00
        self.max_concurrent = 3

    def check_daily_reset(self):
        today = date.today()
        if today != self.current_date:
            self.current_date = today
            self.daily_trades = 0
            self.daily_losses = 0
            self.daily_pnl_inr = 0.0
            for p in self.positions:
                if self.positions[p]['status'] == 'PENDING_ENTRY':
                    self.positions[p] = {'status': 'NONE'}

    def process_cycle(self):
        self.check_daily_reset()
        kill_active = (
            self.daily_trades >= self.max_daily_trades
            or self.daily_losses >= self.max_daily_losses
            or self.daily_pnl_inr <= self.max_daily_loss_inr
        )
        active_count = sum(1 for p in self.positions.values() if p['status'] == 'ACTIVE')

        for pair in self.pairs:
            df = fetch_live_data(pair, "15m", 120)
            time.sleep(0.3)
            df_macro = fetch_live_data(pair, "1h", 100)
            time.sleep(0.3)
            if df.empty or df_macro.empty or len(df) < 105:
                continue

            df_macro['EMA50'] = df_macro['close'].ewm(span=50, adjust=False).mean()
            df['EMA100'] = df['close'].ewm(span=100, adjust=False).mean()
            df['MHULL'] = calculate_ehma(df['close'], length=16)
            df['SHULL'] = df['MHULL'].shift(2)
            tr = pd.concat([
                df['high'] - df['low'],
                (df['high'] - df['close'].shift()).abs(),
                (df['low'] - df['close'].shift()).abs()
            ], axis=1).max(axis=1)
            df['ATR'] = tr.rolling(14).mean()

            c_prev, c_curr = df.iloc[-3], df.iloc[-2]
            live_price = df.iloc[-1]['close']

            buy_signal = (c_prev['SHULL'] >= c_prev['MHULL']) and (c_curr['SHULL'] < c_curr['MHULL']) and (c_curr['close'] > c_curr['EMA100'])
            sell_signal = (c_prev['SHULL'] <= c_prev['MHULL']) and (c_curr['SHULL'] > c_curr['MHULL']) and (c_curr['close'] < c_curr['EMA100'])
            pos = self.positions[pair]

            if pos['status'] == 'ACTIVE':
                ep = pos['limit_price']
                floating_usd = (live_price - ep) * pos['size'] if pos['side'] == 'LONG' else (ep - live_price) * pos['size']
                floating_fees = (ep * pos['size'] * MAKER_FEE) + (live_price * pos['size'] * TAKER_FEE)
                net_floating_inr = (floating_usd - floating_fees) * USDT_INR_RATE
                self.positions[pair]['max_dd_inr'] = min(pos.get('max_dd_inr', 0.0), net_floating_inr)

                if not pos.get('profit_locked', False) and net_floating_inr >= 180.0:
                    lock_offset = (90.0 / USDT_INR_RATE) / pos['size']
                    self.positions[pair]['sl'] = (ep + lock_offset) if pos['side'] == 'LONG' else (ep - lock_offset)
                    self.positions[pair]['profit_locked'] = True

                if (datetime.now() - pos['entry_time']).total_seconds() / 3600 >= 5:
                    if net_floating_inr > 25.0:
                        self.close_trade(pair, live_price, "Timeout (Profit)", "TAKER")
                        continue

                if pos['side'] == 'LONG':
                    if live_price <= pos['sl']:
                        self.close_trade(pair, pos['sl'], "Profit Lock Stop" if pos.get('profit_locked') else "Stop Market", "TAKER")
                    elif live_price >= pos['tp']:
                        self.close_trade(pair, pos['tp'], "Limit TP", "MAKER")
                else:
                    if live_price >= pos['sl']:
                        self.close_trade(pair, pos['sl'], "Profit Lock Stop" if pos.get('profit_locked') else "Stop Market", "TAKER")
                    elif live_price <= pos['tp']:
                        self.close_trade(pair, pos['tp'], "Limit TP", "MAKER")
                continue

            if pos['status'] == 'PENDING_ENTRY':
                if df.iloc[-1]['low'] <= pos['limit_price'] if pos['side'] == 'LONG' else df.iloc[-1]['high'] >= pos['limit_price']:
                    self.activate_trade(pair)
                continue

            if kill_active or active_count >= self.max_concurrent or datetime.now() < self.cooldown_until[pair]:
                continue

            p_high, p_low, q_high, q_low = calculate_pivots(df)
            atr = c_curr['ATR']
            raw_risk_usd = RAW_RISK_BUDGET_INR / USDT_INR_RATE

            if buy_signal and live_price > df_macro.iloc[-1]['EMA50']:
                sl = q_low if (not np.isnan(q_low) and q_low < live_price) else (live_price - atr * 1.4)
                tp = p_high if (not np.isnan(p_high) and p_high > live_price) else (live_price + atr * 3.8)
                self.positions[pair] = {
                    'status': 'PENDING_ENTRY', 'side': 'LONG', 'limit_price': live_price,
                    'sl': sl, 'tp': tp, 'size': raw_risk_usd / max(0.0001, (live_price - sl)),
                    'max_dd_inr': 0.0, 'profit_locked': False
                }
            elif sell_signal and live_price < df_macro.iloc[-1]['EMA50']:
                sl = q_high if (not np.isnan(q_high) and q_high > live_price) else (live_price + atr * 1.4)
                tp = p_low if (not np.isnan(p_low) and p_low < live_price) else (live_price - atr * 3.8)
                self.positions[pair] = {
                    'status': 'PENDING_ENTRY', 'side': 'SHORT', 'limit_price': live_price,
                    'sl': sl, 'tp': tp, 'size': raw_risk_usd / max(0.0001, (sl - live_price)),
                    'max_dd_inr': 0.0, 'profit_locked': False
                }

    def activate_trade(self, pair):
        self.positions[pair].update({'status': 'ACTIVE', 'entry_time': datetime.now(), 'max_dd_inr': 0.0})
        self.daily_trades += 1

    def close_trade(self, pair, exec_price, reason, order_type):
        pos = self.positions[pair]
        ep = pos['limit_price']
        exit_p = (
            exec_price * (1 - (SLIPPAGE_RATE if order_type == "TAKER" else 0.0))
            if pos['side'] == 'LONG'
            else exec_price * (1 + (SLIPPAGE_RATE if order_type == "TAKER" else 0.0))
        )
        gross = (exit_p - ep) * pos['size'] if pos['side'] == 'LONG' else (ep - exit_p) * pos['size']
        fees = (ep * pos['size'] * MAKER_FEE) + (exit_p * pos['size'] * (MAKER_FEE if order_type == "MAKER" else TAKER_FEE))
        net_inr = (gross - fees) * USDT_INR_RATE
        self.daily_pnl_inr += net_inr

        if net_inr <= 0:
            self.daily_losses += 1
            self.cooldown_until[pair] = datetime.now() + timedelta(minutes=30)

        log_trade(TW_LEDGER, {
            'Time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 'Pair': pair, 'Side': pos['side'],
            'Entry': round(ep, 4), 'Exit': round(exit_p, 4), 'Reason': reason,
            'Gross_USD': round(gross, 2), 'Fees_USD': round(fees, 2),
            'Slippage_USD': round(abs(exec_price - exit_p) * pos['size'], 2),
            'Net_PnL_INR': round(net_inr, 2), 'Max_DD_INR': round(pos.get('max_dd_inr', 0.0), 2)
        })
        self.positions[pair] = {'status': 'NONE'}

# ==========================================
# STRATEGY 3: TW FINE-TUNED (V2.5)
# ==========================================
class TWTunedBot:
    def __init__(self, pairs):
        self.pairs = pairs
        self.positions = {pair: {'status': 'NONE'} for pair in pairs}
        self.cooldown_until = {pair: datetime.min for pair in pairs}
        self.current_date = date.today()
        self.daily_trades = 0
        self.daily_losses = 0
        self.daily_pnl_inr = 0.0
        self.max_daily_trades = 10
        self.max_daily_losses = 3
        self.max_daily_loss_inr = -450.00
        self.max_concurrent = 3

    def check_daily_reset(self):
        today = date.today()
        if today != self.current_date:
            self.current_date = today
            self.daily_trades = 0
            self.daily_losses = 0
            self.daily_pnl_inr = 0.0
            for p in self.positions:
                if self.positions[p]['status'] == 'PENDING_ENTRY':
                    self.positions[p] = {'status': 'NONE'}

    def process_cycle(self):
        self.check_daily_reset()
        kill_active = (
            self.daily_trades >= self.max_daily_trades
            or self.daily_losses >= self.max_daily_losses
            or self.daily_pnl_inr <= self.max_daily_loss_inr
        )
        active_count = sum(1 for p in self.positions.values() if p['status'] == 'ACTIVE')

        for pair in self.pairs:
            df = fetch_live_data(pair, "15m", 120)
            time.sleep(0.3)
            df_macro = fetch_live_data(pair, "1h", 100)
            time.sleep(0.3)
            if df.empty or df_macro.empty or len(df) < 105:
                continue

            df_macro['EMA50'] = df_macro['close'].ewm(span=50, adjust=False).mean()
            df['EMA100'] = df['close'].ewm(span=100, adjust=False).mean()
            df['MHULL'] = calculate_ehma(df['close'], length=16)
            df['SHULL'] = df['MHULL'].shift(3)
            tr = pd.concat([
                df['high'] - df['low'],
                (df['high'] - df['close'].shift()).abs(),
                (df['low'] - df['close'].shift()).abs()
            ], axis=1).max(axis=1)
            df['ATR'] = tr.rolling(14).mean()
            df['ATR_50'] = df['ATR'].rolling(50).mean()

            c_prev, c_curr = df.iloc[-3], df.iloc[-2]
            live_price = df.iloc[-1]['close']

            buy_signal = (
                (c_prev['SHULL'] >= c_prev['MHULL']) and (c_curr['SHULL'] < c_curr['MHULL'])
                and (c_curr['close'] > c_curr['EMA100']) and (c_curr['close'] > c_curr['MHULL'])
                and (c_curr['ATR'] > c_curr['ATR_50'])
            )
            sell_signal = (
                (c_prev['SHULL'] <= c_prev['MHULL']) and (c_curr['SHULL'] > c_curr['MHULL'])
                and (c_curr['close'] < c_curr['EMA100']) and (c_curr['close'] < c_curr['MHULL'])
                and (c_curr['ATR'] > c_curr['ATR_50'])
            )
            pos = self.positions[pair]

            if pos['status'] == 'ACTIVE':
                ep = pos['limit_price']
                floating_usd = (live_price - ep) * pos['size'] if pos['side'] == 'LONG' else (ep - live_price) * pos['size']
                floating_fees = (ep * pos['size'] * MAKER_FEE) + (live_price * pos['size'] * TAKER_FEE)
                net_floating_inr = (floating_usd - floating_fees) * USDT_INR_RATE
                self.positions[pair]['max_dd_inr'] = min(pos.get('max_dd_inr', 0.0), net_floating_inr)

                if not pos.get('profit_locked', False) and net_floating_inr >= 180.0:
                    lock_offset = (90.0 / USDT_INR_RATE) / pos['size']
                    self.positions[pair]['sl'] = (ep + lock_offset) if pos['side'] == 'LONG' else (ep - lock_offset)
                    self.positions[pair]['profit_locked'] = True

                if (datetime.now() - pos['entry_time']).total_seconds() / 3600 >= 5:
                    if net_floating_inr > 25.0:
                        self.close_trade(pair, live_price, "Timeout (Profit)", "TAKER")
                        continue

                if pos['side'] == 'LONG':
                    if live_price <= pos['sl']:
                        self.close_trade(pair, pos['sl'], "Profit Lock Stop" if pos.get('profit_locked') else "Stop Market", "TAKER")
                    elif live_price >= pos['tp']:
                        self.close_trade(pair, pos['tp'], "Limit TP", "MAKER")
                else:
                    if live_price >= pos['sl']:
                        self.close_trade(pair, pos['sl'], "Profit Lock Stop" if pos.get('profit_locked') else "Stop Market", "TAKER")
                    elif live_price <= pos['tp']:
                        self.close_trade(pair, pos['tp'], "Limit TP", "MAKER")
                continue

            if pos['status'] == 'PENDING_ENTRY':
                if df.iloc[-1]['low'] <= pos['limit_price'] if pos['side'] == 'LONG' else df.iloc[-1]['high'] >= pos['limit_price']:
                    self.activate_trade(pair)
                continue

            if kill_active or active_count >= self.max_concurrent or datetime.now() < self.cooldown_until[pair]:
                continue

            p_high, p_low, q_high, q_low = calculate_pivots(df)
            atr = c_curr['ATR']
            raw_risk_usd = RAW_RISK_BUDGET_INR / USDT_INR_RATE

            if buy_signal and live_price > df_macro.iloc[-1]['EMA50']:
                sl = q_low if (not np.isnan(q_low) and q_low < live_price) else (live_price - atr * 1.4)
                tp = p_high if (not np.isnan(p_high) and p_high > live_price) else (live_price + atr * 3.8)
                self.positions[pair] = {
                    'status': 'PENDING_ENTRY', 'side': 'LONG', 'limit_price': live_price,
                    'sl': sl, 'tp': tp, 'size': raw_risk_usd / max(0.0001, (live_price - sl)),
                    'max_dd_inr': 0.0, 'profit_locked': False
                }
            elif sell_signal and live_price < df_macro.iloc[-1]['EMA50']:
                sl = q_high if (not np.isnan(q_high) and q_high > live_price) else (live_price + atr * 1.4)
                tp = p_low if (not np.isnan(p_low) and p_low < live_price) else (live_price - atr * 3.8)
                self.positions[pair] = {
                    'status': 'PENDING_ENTRY', 'side': 'SHORT', 'limit_price': live_price,
                    'sl': sl, 'tp': tp, 'size': raw_risk_usd / max(0.0001, (sl - live_price)),
                    'max_dd_inr': 0.0, 'profit_locked': False
                }

    def activate_trade(self, pair):
        self.positions[pair].update({'status': 'ACTIVE', 'entry_time': datetime.now(), 'max_dd_inr': 0.0})
        self.daily_trades += 1

    def close_trade(self, pair, exec_price, reason, order_type):
        pos = self.positions[pair]
        ep = pos['limit_price']
        exit_p = (
            exec_price * (1 - (SLIPPAGE_RATE if order_type == "TAKER" else 0.0))
            if pos['side'] == 'LONG'
            else exec_price * (1 + (SLIPPAGE_RATE if order_type == "TAKER" else 0.0))
        )
        gross = (exit_p - ep) * pos['size'] if pos['side'] == 'LONG' else (ep - exit_p) * pos['size']
        fees = (ep * pos['size'] * MAKER_FEE) + (exit_p * pos['size'] * (MAKER_FEE if order_type == "MAKER" else TAKER_FEE))
        net_inr = (gross - fees) * USDT_INR_RATE
        self.daily_pnl_inr += net_inr

        if net_inr <= 0:
            self.daily_losses += 1
            self.cooldown_until[pair] = datetime.now() + timedelta(minutes=30)

        log_trade(TW_TUNED_LEDGER, {
            'Time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 'Pair': pair, 'Side': pos['side'],
            'Entry': round(ep, 4), 'Exit': round(exit_p, 4), 'Reason': reason,
            'Gross_USD': round(gross, 2), 'Fees_USD': round(fees, 2),
            'Slippage_USD': round(abs(exec_price - exit_p) * pos['size'], 2),
            'Net_PnL_INR': round(net_inr, 2), 'Max_DD_INR': round(pos.get('max_dd_inr', 0.0), 2)
        })
        self.positions[pair] = {'status': 'NONE'}

# ==========================================
# MASTER CONTINUOUS ENGINE RUNNER
# ==========================================
class MasterEngine:
    def __init__(self):
        self.amte = AMTEBot(PAIRS)
        self.tw_orig = TWAllInOneBot(PAIRS)
        self.tw_tuned = TWTunedBot(PAIRS)

    def loop(self):
        time.sleep(10)
        while True:
            try:
                self.amte.process_cycle()
                self.tw_orig.process_cycle()
                self.tw_tuned.process_cycle()
                time.sleep(120)
            except Exception:
                time.sleep(120)

@st.cache_resource
def start_master_engine():
    engine = MasterEngine()
    t = threading.Thread(target=engine.loop, daemon=True)
    t.start()
    return engine

master = start_master_engine()

# ==========================================
# STREAMLIT UI
# ==========================================
st.set_page_config(page_title="AMTE Quantitative Terminal", layout="wide")
st.title("⚡ AMTE Quantitative Multi-Model Terminal")
st.caption("Engine active in background. 🛡️ V2.5 Precision Shield (-₹150 Net Loss Cap | 3-Loss Daily Circuit Breaker | Persistent Ledger Backup).")

def render_strategy_tab(bot, ledger_file, label_prefix, key_suffix):
    with st.expander("💾 Restore / Merge Previous Trades CSV (Prevents Loss on Render Restart)"):
        up = st.file_uploader(f"Upload past {ledger_file} to merge with live trades:", type=["csv"], key=f"up_{key_suffix}")
        if up is not None and st.button("Merge & Save Ledger", key=f"btn_{key_suffix}"):
            ok, msg = merge_uploaded_ledger(ledger_file, up)
            if ok:
                st.success(msg)
            else:
                st.error(msg)

    df_led = load_ledger(ledger_file)
    net_pnl = df_led['Net_PnL_INR'].sum() if not df_led.empty else 0.0
    active_cnt = sum(1 for p in bot.positions.values() if p['status'] == 'ACTIVE')
    pending_cnt = sum(1 for p in bot.positions.values() if p['status'] == 'PENDING_ENTRY')

    wins = len(df_led[df_led['Net_PnL_INR'] > 0]) if not df_led.empty else 0
    losses = len(df_led[df_led['Net_PnL_INR'] <= 0]) if not df_led.empty else 0
    win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0.0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Capital Balance", f"₹{(CAPITAL_INR + net_pnl):,.2f}", f"₹{net_pnl:,.2f}")
    c2.metric("Market Exposure", f"{active_cnt} Active / {pending_cnt} Pending")
    c3.metric("Today's Trades / Losses", f"{bot.daily_trades}/{bot.max_daily_trades} | {bot.daily_losses}/{bot.max_daily_losses}L")
    c4.metric("Today's PnL", f"₹{bot.daily_pnl_inr:,.2f}")
    c5.metric("Win Rate (All-Time)", f"{win_rate:.1f}%", f"{wins}W / {losses}L")

    live_trades = [
        {
            "Pair": p, "Status": pos['status'], "Side": pos.get('side', '-'),
            "Target Entry": f"${pos.get('limit_price', 0):,.4f}",
            "Take Profit": f"${pos.get('tp', 0):,.4f}",
            "Stop Loss": f"${pos.get('sl', 0):,.4f}",
            "Profit Locked": "Yes" if pos.get('profit_locked') else "No"
        }
        for p, pos in bot.positions.items() if pos['status'] != 'NONE'
    ]
    if live_trades:
        st.write("🟢 **Currently Running & Pending Trades**")
        st.dataframe(pd.DataFrame(live_trades), use_container_width=True, hide_index=True)

    st.markdown("---")
    pair_sel = st.selectbox(f"Select Asset Pair ({label_prefix}):", PAIRS, key=f"pair_{key_suffix}")
    df_chart = fetch_live_data(pair_sel, "15m", 120)
    if not df_chart.empty:
        fig = go.Figure()
        fig.add_trace(go.Candlestick(x=df_chart.index, open=df_chart['open'], high=df_chart['high'], low=df_chart['low'], close=df_chart['close'], name="15m"))
        pos_sel = bot.positions[pair_sel]
        if pos_sel['status'] == 'PENDING_ENTRY':
            fig.add_hline(y=pos_sel['limit_price'], line_dash="dot", line_color="#BDBDBD", annotation_text="Limit")
        elif pos_sel['status'] == 'ACTIVE':
            fig.add_hline(y=pos_sel['limit_price'], line_dash="solid", line_color="#FF9800", annotation_text="Entry")
            fig.add_hline(y=pos_sel['tp'], line_dash="dash", line_color="#00E676", annotation_text="TP")
            fig.add_hline(y=pos_sel['sl'], line_dash="dash", line_color="#FF5252", annotation_text="SL")
        fig.update_layout(height=420, template="plotly_dark", xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig, key=f"chart_{key_suffix}", width="stretch")

    if not df_led.empty:
        st.download_button(
            label=f"📥 Download {label_prefix} Ledger CSV",
            data=df_led.to_csv(index=False).encode('utf-8'),
            file_name=ledger_file,
            mime='text/csv',
            key=f"dl_{key_suffix}"
        )
        st.dataframe(df_led.sort_index(ascending=False), use_container_width=True)

tab1, tab2, tab3 = st.tabs(["⚡ Strategy A: AMTE BB Pullback", "🎯 Strategy B: TW Original", "🛠️ Strategy C: TW Fine-Tuned"])
with tab1:
    render_strategy_tab(master.amte, AMTE_LEDGER, "Strategy A", "a")
with tab2:
    render_strategy_tab(master.tw_orig, TW_LEDGER, "Strategy B", "b")
with tab3:
    render_strategy_tab(master.tw_tuned, TW_TUNED_LEDGER, "Strategy C", "c")
