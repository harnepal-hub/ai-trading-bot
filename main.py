import os
import time
import threading
import warnings
from datetime import datetime
import requests
import pandas as pd
import numpy as np
import streamlit as st
import plotly.graph_objects as go

warnings.filterwarnings('ignore')

# --- CONFIGURATION ---
PAIRS = ["B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT"]
INITIAL_CAPITAL = 1200.00
TRADE_LOG_FILE = "trade_ledger.csv"

def fetch_live_data(pair, interval, limit=100):
    url = f"https://public.coindcx.com/market_data/candles?pair={pair}&interval={interval}&limit={limit}"
    try:
        response = requests.get(url, timeout=10)
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

def log_trade_to_csv(trade_data):
    df_new = pd.DataFrame([trade_data])
    if os.path.exists(TRADE_LOG_FILE):
        df_new.to_csv(TRADE_LOG_FILE, mode='a', header=False, index=False)
    else:
        df_new.to_csv(TRADE_LOG_FILE, mode='w', header=True, index=False)

# ==========================================
# BACKGROUND BOT ENGINE
# ==========================================
class VisualEvolvingBot:
    def __init__(self, pairs, capital):
        self.pairs = pairs
        self.capital = capital
        self.positions = {pair: None for pair in pairs}
        self.dna_bb_std = {pair: 2.0 for pair in pairs}
        
        if not os.path.exists(TRADE_LOG_FILE):
            pd.DataFrame(columns=['Time', 'Pair', 'Side', 'Entry', 'Exit', 'Reason', 'PnL']).to_csv(TRADE_LOG_FILE, index=False)

    def run_loop(self):
        time.sleep(5)
        while True:
            try:
                current_bal = self.get_current_balance()
                
                for pair in self.pairs:
                    df_1h = fetch_live_data(pair, "1h", 100)
                    time.sleep(1)
                    df_15m = fetch_live_data(pair, "15m", 100)
                    time.sleep(1)
                    
                    if df_1h.empty or df_15m.empty: continue
                        
                    df_1h['EMA_20'] = df_1h['close'].ewm(span=20, adjust=False).mean()
                    df_1h['EMA_50'] = df_1h['close'].ewm(span=50, adjust=False).mean()
                    df_1h['ema_spread'] = (df_1h['EMA_20'] - df_1h['EMA_50']).abs() / df_1h['close'] * 100
                    
                    latest_1h = df_1h.iloc[-2]
                    is_bull = (latest_1h['EMA_20'] > latest_1h['EMA_50']) and (latest_1h['ema_spread'] > 0.15)
                    is_bear = (latest_1h['EMA_20'] < latest_1h['EMA_50']) and (latest_1h['ema_spread'] > 0.15)

                    std_dev = self.dna_bb_std[pair]
                    df_15m['SMA_20'] = df_15m['close'].rolling(window=20).mean()
                    df_15m['STD_20'] = df_15m['close'].rolling(window=20).std()
                    df_15m['BBL'] = df_15m['SMA_20'] - (df_15m['STD_20'] * std_dev)
                    df_15m['BBU'] = df_15m['SMA_20'] + (df_15m['STD_20'] * std_dev)
                    
                    high_low = df_15m['high'] - df_15m['low']
                    high_close = (df_15m['high'] - df_15m['close'].shift()).abs()
                    low_close = (df_15m['low'] - df_15m['close'].shift()).abs()
                    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
                    df_15m['ATR'] = tr.rolling(window=14).mean()
                    
                    live_candle = df_15m.iloc[-1]
                    close = live_candle['close']
                    atr = live_candle['ATR']
                    
                    if self.positions[pair] is not None:
                        pos = self.positions[pair]
                        side, ep, sl, tp, size = pos['side'], pos['entry'], pos['sl'], pos['tp'], pos['size']
                        
                        if side == 'LONG' and not pos['be_moved'] and live_candle['high'] >= pos['be_trigger']:
                            self.positions[pair]['sl'] = ep
                            self.positions[pair]['be_moved'] = True
                        elif side == 'SHORT' and not pos['be_moved'] and live_candle['low'] <= pos['be_trigger']:
                            self.positions[pair]['sl'] = ep
                            self.positions[pair]['be_moved'] = True
                        
                        if side == 'LONG':
                            if live_candle['low'] <= sl: self.close_trade(pair, sl, "Stop Loss", pos['be_moved'])
                            elif live_candle['high'] >= tp: self.close_trade(pair, tp, "Take Profit", pos['be_moved'])
                        else:
                            if live_candle['high'] >= sl: self.close_trade(pair, sl, "Stop Loss", pos['be_moved'])
                            elif live_candle['low'] <= tp: self.close_trade(pair, tp, "Take Profit", pos['be_moved'])
                        continue

                    risk_amt = current_bal * 0.01
                    if is_bull and close <= live_candle['BBL']:
                        sl, tp = close - (atr * 2.0), close + (atr * 4.0)
                        self.open_trade(pair, 'LONG', close, sl, tp, risk_amt / (close - sl), close + (atr * 2.0))
                    elif is_bear and close >= live_candle['BBU']:
                        sl, tp = close + (atr * 2.0), close - (atr * 4.0)
                        self.open_trade(pair, 'SHORT', close, sl, tp, risk_amt / (sl - close), close - (atr * 2.0))
                        
                time.sleep(60)
            except Exception as e:
                time.sleep(60)

    def open_trade(self, pair, side, ep, sl, tp, size, be_trigger):
        self.positions[pair] = {'side': side, 'entry': ep, 'sl': sl, 'tp': tp, 'size': size, 'be_trigger': be_trigger, 'be_moved': False}

    def close_trade(self, pair, exit_price, reason, is_be):
        pos = self.positions[pair]
        gross = (exit_price - pos['entry']) * pos['size'] if pos['side'] == 'LONG' else (pos['entry'] - exit_price) * pos['size']
        net_pnl = gross - ((exit_price * pos['size']) * 0.002)
        
        log_trade_to_csv({
            'Time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'Pair': pair,
            'Side': pos['side'],
            'Entry': round(pos['entry'], 2),
            'Exit': round(exit_price, 2),
            'Reason': reason,
            'PnL': round(net_pnl, 2)
        })
        
        if not is_be:
            if net_pnl > 0: self.dna_bb_std[pair] = max(1.7, self.dna_bb_std[pair] - 0.1)
            else: self.dna_bb_std[pair] = min(2.8, self.dna_bb_std[pair] + 0.2)
        self.positions[pair] = None

    def get_current_balance(self):
        if os.path.exists(TRADE_LOG_FILE):
            df = pd.read_csv(TRADE_LOG_FILE)
            if not df.empty and 'PnL' in df.columns:
                return INITIAL_CAPITAL + df['PnL'].sum()
        return INITIAL_CAPITAL

@st.cache_resource
def start_background_bot():
    bot = VisualEvolvingBot(PAIRS, INITIAL_CAPITAL)
    t = threading.Thread(target=bot.run_loop, daemon=True)
    t.start()
    return bot

bot_instance = start_background_bot()

# ==========================================
# STREAMLIT DASHBOARD UI
# ==========================================
st.set_page_config(page_title="AI Trading Terminal", layout="wide")
st.title("⚡ Autonomous Quantitative Trading Terminal")

current_bal = bot_instance.get_current_balance()
net_profit = current_bal - INITIAL_CAPITAL
df_ledger = pd.read_csv(TRADE_LOG_FILE) if os.path.exists(TRADE_LOG_FILE) else pd.DataFrame()

total_trades = len(df_ledger)
wins = len(df_ledger[df_ledger['PnL'] > 0]) if total_trades > 0 else 0
win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

col1, col2, col3, col4 = st.columns(4)
col1.metric("Account Balance", f"${current_bal:,.2f}", f"${net_profit:,.2f}")
col2.metric("Total Net Profit", f"${net_profit:,.2f}")
col3.metric("Win Rate", f"{win_rate:.1f}%", f"{wins}/{total_trades} Trades")
col4.metric("Active AI DNA Stretch", "Dynamic (1.7 - 2.8)")

st.markdown("---")

st.subheader("📊 Live Market Strategy Charts & Bollinger Bands")
selected_pair = st.selectbox("Select Asset Pair:", PAIRS)

df_chart = fetch_live_data(selected_pair, "15m", 100)
if not df_chart.empty:
    std_dev = bot_instance.dna_bb_std[selected_pair]
    df_chart['SMA20'] = df_chart['close'].rolling(20).mean()
    df_chart['STD'] = df_chart['close'].rolling(20).std()
    df_chart['BBL'] = df_chart['SMA20'] - (df_chart['STD'] * std_dev)
    df_chart['BBU'] = df_chart['SMA20'] + (df_chart['STD'] * std_dev)

    fig = go.Figure()
    fig.add_trace(go.Candlestick(x=df_chart.index, open=df_chart['open'], high=df_chart['high'], low=df_chart['low'], close=df_chart['close'], name="Candles"))
    fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart['BBU'], line=dict(color='gray', width=1), name="Upper BB"))
    fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart['BBL'], line=dict(color='gray', width=1), fill='tonexty', name="Lower BB (Dip Zone)"))
    fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart['SMA20'], line=dict(color='blue', width=1.5), name="SMA 20"))

    active_pos = bot_instance.positions[selected_pair]
    if active_pos:
        fig.add_hline(y=active_pos['entry'], line_dash="solid", line_color="orange", annotation_text="Entry Price")
        fig.add_hline(y=active_pos['tp'], line_dash="dash", line_color="green", annotation_text="Take Profit")
        fig.add_hline(y=active_pos['sl'], line_dash="dash", line_color="red", annotation_text="Stop Loss")

    fig.update_layout(height=500, template="plotly_dark", margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(fig, use_container_width=True)
else:
    st.warning("Fetching chart data from exchange...")

st.markdown("---")

st.subheader("📜 Complete Trade Ledger History")
if not df_ledger.empty:
    st.dataframe(df_ledger.sort_index(ascending=False), use_container_width=True)
else:
    st.info("No closed trades recorded yet. The bot is scanning for high-probability pullbacks...")        df_new.to_csv(TRADE_LOG_FILE, mode='a', header=False, index=False)
    else:
        df_new.to_csv(TRADE_LOG_FILE, mode='w', header=True, index=False)

# ==========================================
# BACKGROUND BOT ENGINE
# ==========================================
class VisualEvolvingBot:
    def __init__(self, pairs, capital):
        self.pairs = pairs
        self.capital = capital
        self.positions = {pair: None for pair in pairs}
        self.dna_bb_std = {pair: 2.0 for pair in pairs}
        
        if not os.path.exists(TRADE_LOG_FILE):
            pd.DataFrame(columns=['Time', 'Pair', 'Side', 'Entry', 'Exit', 'Reason', 'PnL']).to_csv(TRADE_LOG_FILE, index=False)

    def run_loop(self):
        time.sleep(5)
        while True:
            try:
                current_bal = self.get_current_balance()
                
                for pair in self.pairs:
                    df_1h = fetch_live_data(pair, "1h", 100)
                    time.sleep(1)
                    df_15m = fetch_live_data(pair, "15m", 100)
                    time.sleep(1)
                    
                    if df_1h.empty or df_15m.empty: continue
                        
                    df_1h['EMA_20'] = df_1h['close'].ewm(span=20, adjust=False).mean()
                    df_1h['EMA_50'] = df_1h['close'].ewm(span=50, adjust=False).mean()
                    df_1h['ema_spread'] = (df_1h['EMA_20'] - df_1h['EMA_50']).abs() / df_1h['close'] * 100
                    
                    latest_1h = df_1h.iloc[-2]
                    is_bull = (latest_1h['EMA_20'] > latest_1h['EMA_50']) and (latest_1h['ema_spread'] > 0.15)
                    is_bear = (latest_1h['EMA_20'] < latest_1h['EMA_50']) and (latest_1h['ema_spread'] > 0.15)

                    std_dev = self.dna_bb_std[pair]
                    df_15m['SMA_20'] = df_15m['close'].rolling(window=20).mean()
                    df_15m['STD_20'] = df_15m['close'].rolling(window=20).std()
                    df_15m['BBL'] = df_15m['SMA_20'] - (df_15m['STD_20'] * std_dev)
                    df_15m['BBU'] = df_15m['SMA_20'] + (df_15m['STD_20'] * std_dev)
                    
                    high_low = df_15m['high'] - df_15m['low']
                    high_close = (df_15m['high'] - df_15m['close'].shift()).abs()
                    low_close = (df_15m['low'] - df_15m['close'].shift()).abs()
                    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
                    df_15m['ATR'] = tr.rolling(window=14).mean()
                    
                    live_candle = df_15m.iloc[-1]
                    close = live_candle['close']
                    atr = live_candle['ATR']
                    
                    if self.positions[pair] is not None:
                        pos = self.positions[pair]
                        side, ep, sl, tp, size = pos['side'], pos['entry'], pos['sl'], pos['tp'], pos['size']
                        
                        if side == 'LONG' and not pos['be_moved'] and live_candle['high'] >= pos['be_trigger']:
                            self.positions[pair]['sl'] = ep
                            self.positions[pair]['be_moved'] = True
                        elif side == 'SHORT' and not pos['be_moved'] and live_candle['low'] <= pos['be_trigger']:
                            self.positions[pair]['sl'] = ep
                            self.positions[pair]['be_moved'] = True
                        
                        if side == 'LONG':
                            if live_candle['low'] <= sl: self.close_trade(pair, sl, "Stop Loss", pos['be_moved'])
                            elif live_candle['high'] >= tp: self.close_trade(pair, tp, "Take Profit", pos['be_moved'])
                        else:
                            if live_candle['high'] >= sl: self.close_trade(pair, sl, "Stop Loss", pos['be_moved'])
                            elif live_candle['low'] <= tp: self.close_trade(pair, tp, "Take Profit", pos['be_moved'])
                        continue

                    risk_amt = current_bal * 0.01
                    if is_bull and close <= live_candle['BBL']:
                        sl, tp = close - (atr * 2.0), close + (atr * 4.0)
                        self.open_trade(pair, 'LONG', close, sl, tp, risk_amt / (close - sl), close + (atr * 2.0))
                    elif is_bear and close >= live_candle['BBU']:
                        sl, tp = close + (atr * 2.0), close - (atr * 4.0)
                        self.open_trade(pair, 'SHORT', close, sl, tp, risk_amt / (sl - close), close - (atr * 2.0))
                        
                time.sleep(60)
            except Exception as e:
                time.sleep(60)

    def open_trade(self, pair, side, ep, sl, tp, size, be_trigger):
        self.positions[pair] = {'side': side, 'entry': ep, 'sl': sl, 'tp': tp, 'size': size, 'be_trigger': be_trigger, 'be_moved': False}

    def close_trade(self, pair, exit_price, reason, is_be):
        pos = self.positions[pair]
        gross = (exit_price - pos['entry']) * pos['size'] if pos['side'] == 'LONG' else (pos['entry'] - exit_price) * pos['size']
        net_pnl = gross - ((exit_price * pos['size']) * 0.002)
        
        log_trade_to_csv({
            'Time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'Pair': pair,
            'Side': pos['side'],
            'Entry': round(pos['entry'], 2),
            'Exit': round(exit_price, 2),
            'Reason': reason,
            'PnL': round(net_pnl, 2)
        })
        
        if not is_be:
            if net_pnl > 0: self.dna_bb_std[pair] = max(1.7, self.dna_bb_std[pair] - 0.1)
            else: self.dna_bb_std[pair] = min(2.8, self.dna_bb_std[pair] + 0.2)
        self.positions[pair] = None

    def get_current_balance(self):
        if os.path.exists(TRADE_LOG_FILE):
            df = pd.read_csv(TRADE_LOG_FILE)
            if not df.empty and 'PnL' in df.columns:
                return INITIAL_CAPITAL + df['PnL'].sum()
        return INITIAL_CAPITAL

@st.cache_resource
def start_background_bot():
    bot = VisualEvolvingBot(PAIRS, INITIAL_CAPITAL)
    t = threading.Thread(target=bot.run_loop, daemon=True)
    t.start()
    return bot

bot_instance = start_background_bot()

# ==========================================
# STREAMLIT DASHBOARD UI
# ==========================================
st.set_page_config(page_title="AI Trading Terminal", layout="wide")
st.title("⚡ Autonomous Quantitative Trading Terminal")

current_bal = bot_instance.get_current_balance()
net_profit = current_bal - INITIAL_CAPITAL
df_ledger = pd.read_csv(TRADE_LOG_FILE) if os.path.exists(TRADE_LOG_FILE) else pd.DataFrame()

total_trades = len(df_ledger)
wins = len(df_ledger[df_ledger['PnL'] > 0]) if total_trades > 0 else 0
win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

col1, col2, col3, col4 = st.columns(4)
col1.metric("Account Balance", f"${current_bal:,.2f}", f"${net_profit:,.2f}")
col2.metric("Total Net Profit", f"${net_profit:,.2f}")
col3.metric("Win Rate", f"{win_rate:.1f}%", f"{wins}/{total_trades} Trades")
col4.metric("Active AI DNA Stretch", "Dynamic (1.7 - 2.8)")

st.markdown("---")

st.subheader("📊 Live Market Strategy Charts & Bollinger Bands")
selected_pair = st.selectbox("Select Asset Pair:", PAIRS)

df_chart = fetch_live_data(selected_pair, "15m", 100)
if not df_chart.empty:
    std_dev = bot_instance.dna_bb_std[selected_pair]
    df_chart['SMA20'] = df_chart['close'].rolling(20).mean()
    df_chart['STD'] = df_chart['close'].rolling(20).std()
    df_chart['BBL'] = df_chart['SMA20'] - (df_chart['STD'] * std_dev)
    df_chart['BBU'] = df_chart['SMA20'] + (df_chart['STD'] * std_dev)

    fig = go.Figure()
    fig.add_trace(go.Candlestick(x=df_chart.index, open=df_chart['open'], high=df_chart['high'], low=df_chart['low'], close=df_chart['close'], name="Candles"))
    fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart['BBU'], line=dict(color='gray', width=1), name="Upper BB"))
    fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart['BBL'], line=dict(color='gray', width=1), fill='tonexty', name="Lower BB (Dip Zone)"))
    fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart['SMA20'], line=dict(color='blue', width=1.5), name="SMA 20"))

    active_pos = bot_instance.positions[selected_pair]
    if active_pos:
        fig.add_hline(y=active_pos['entry'], line_dash="solid", line_color="orange", annotation_text="Entry Price")
        fig.add_hline(y=active_pos['tp'], line_dash="dash", line_color="green", annotation_text="Take Profit")
        fig.add_hline(y=active_pos['sl'], line_dash="dash", line_color="red", annotation_text="Stop Loss")

    fig.update_layout(height=500, template="plotly_dark", margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(fig, use_container_width=True)
else:
    st.warning("Fetching chart data from exchange...")

st.markdown("---")

st.subheader("📜 Complete Trade Ledger History")
if not df_ledger.empty:
    st.dataframe(df_ledger.sort_index(ascending=False), use_container_width=True)
else:
    st.info("No closed trades recorded yet. The bot is scanning for high-probability pullbacks...")        df_new.to_csv(TRADE_LOG_FILE, mode='a', header=False, index=False)
    else:
        df_new.to_csv(TRADE_LOG_FILE, mode='w', header=True, index=False)

# ==========================================
# BACKGROUND BOT ENGINE
# ==========================================
class VisualEvolvingBot:
    def __init__(self, pairs, capital):
        self.pairs = pairs
        self.capital = capital
        self.positions = {pair: None for pair in pairs}
        self.dna_bb_std = {pair: 2.0 for pair in pairs}
        
        # Initialize ledger file if missing
        if not os.path.exists(TRADE_LOG_FILE):
            pd.DataFrame(columns=['Time', 'Pair', 'Side', 'Entry', 'Exit', 'Reason', 'PnL']).to_csv(TRADE_LOG_FILE, index=False)

    def run_loop(self):
        time.sleep(5)
        while True:
            try:
                # Read current balance from ledger or start default
                current_bal = self.get_current_balance()
                
                for pair in self.pairs:
                    df_1h = fetch_live_data(pair, "1h", 100)
                    time.sleep(1)
                    df_15m = fetch_live_data(pair, "15m", 100)
                    time.sleep(1)
                    
                    if df_1h.empty or df_15m.empty: continue
                        
                    df_1h['EMA_20'] = df_1h['close'].ewm(span=20, adjust=False).mean()
                    df_1h['EMA_50'] = df_1h['close'].ewm(span=50, adjust=False).mean()
                    df_1h['ema_spread'] = (df_1h['EMA_20'] - df_1h['EMA_50']).abs() / df_1h['close'] * 100
                    
                    latest_1h = df_1h.iloc[-2]
                    is_bull = (latest_1h['EMA_20'] > latest_1h['EMA_50']) and (latest_1h['ema_spread'] > 0.15)
                    is_bear = (latest_1h['EMA_20'] < latest_1h['EMA_50']) and (latest_1h['ema_spread'] > 0.15)

                    std_dev = self.dna_bb_std[pair]
                    df_15m['SMA_20'] = df_15m['close'].rolling(window=20).mean()
                    df_15m['STD_20'] = df_15m['close'].rolling(window=20).std()
                    df_15m['BBL'] = df_15m['SMA_20'] - (df_15m['STD_20'] * std_dev)
                    df_15m['BBU'] = df_15m['SMA_20'] + (df_15m['STD_20'] * std_dev)
                    
                    high_low = df_15m['high'] - df_15m['low']
                    high_close = (df_15m['high'] - df_15m['close'].shift()).abs()
                    low_close = (df_15m['low'] - df_15m['close'].shift()).abs()
                    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
                    df_15m['ATR'] = tr.rolling(window=14).mean()
                    
                    live_candle = df_15m.iloc[-1]
                    close = live_candle['close']
                    atr = live_candle['ATR']
                    
                    if self.positions[pair] is not None:
                        pos = self.positions[pair]
                        side, ep, sl, tp, size = pos['side'], pos['entry'], pos['sl'], pos['tp'], pos['size']
                        
                        if side == 'LONG' and not pos['be_moved'] and live_candle['high'] >= pos['be_trigger']:
                            self.positions[pair]['sl'] = ep
                            self.positions[pair]['be_moved'] = True
                        elif side == 'SHORT' and not pos['be_moved'] and live_candle['low'] <= pos['be_trigger']:
                            self.positions[pair]['sl'] = ep
                            self.positions[pair]['be_moved'] = True
                        
                        if side == 'LONG':
                            if live_candle['low'] <= sl: self.close_trade(pair, sl, "Stop Loss", pos['be_moved'])
                            elif live_candle['high'] >= tp: self.close_trade(pair, tp, "Take Profit", pos['be_moved'])
                        else:
                            if live_candle['high'] >= sl: self.close_trade(pair, sl, "Stop Loss", pos['be_moved'])
                            elif live_candle['low'] <= tp: self.close_trade(pair, tp, "Take Profit", pos['be_moved'])
                        continue

                    risk_amt = current_bal * 0.01
                    if is_bull and close <= live_candle['BBL']:
                        sl, tp = close - (atr * 2.0), close + (atr * 4.0)
                        self.open_trade(pair, 'LONG', close, sl, tp, risk_amt / (close - sl), close + (atr * 2.0))
                    elif is_bear and close >= live_candle['BBU']:
                        sl, tp = close + (atr * 2.0), close - (atr * 4.0)
                        self.open_trade(pair, 'SHORT', close, sl, tp, risk_amt / (sl - close), close - (atr * 2.0))
                        
                time.sleep(60)
            except Exception as e:
                time.sleep(60)

    def open_trade(self, pair, side, ep, sl, tp, size, be_trigger):
        self.positions[pair] = {'side': side, 'entry': ep, 'sl': sl, 'tp': tp, 'size': size, 'be_trigger': be_trigger, 'be_moved': False}

    def close_trade(self, pair, exit_price, reason, is_be):
        pos = self.positions[pair]
        gross = (exit_price - pos['entry']) * pos['size'] if pos['side'] == 'LONG' else (pos['entry'] - exit_price) * pos['size']
        net_pnl = gross - ((exit_price * pos['size']) * 0.002)
        
        log_trade_to_csv({
            'Time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'Pair': pair,
            'Side': pos['side'],
            'Entry': round(pos['entry'], 2),
            'Exit': round(exit_price, 2),
            'Reason': reason,
            'PnL': round(net_pnl, 2)
        })
        
        if not is_be:
            if net_pnl > 0: self.dna_bb_std[pair] = max(1.7, self.dna_bb_std[pair] - 0.1)
            else: self.dna_bb_std[pair] = min(2.8, self.dna_bb_std[pair] + 0.2)
        self.positions[pair] = None

    def get_current_balance(self):
        if os.path.exists(TRADE_LOG_FILE):
            df = pd.read_csv(TRADE_LOG_FILE)
            if not df.empty and 'PnL' in df.columns:
                return INITIAL_CAPITAL + df['PnL'].sum()
        return INITIAL_CAPITAL

# Start Bot Thread invisibly on startup
@st.cache_resource
def start_background_bot():
    bot = VisualEvolvingBot(PAIRS, INITIAL_CAPITAL)
    t = threading.Thread(target=bot.run_loop, daemon=True)
    t.start()
    return bot

bot_instance = start_background_bot()

# ==========================================
# STREAMLIT DASHBOARD UI
# ==========================================
st.set_page_config(page_title="AI Trading Terminal", layout="wide")
st.title("⚡ Autonomous Quantitative Trading Terminal")

# Top Metrics Row
current_bal = bot_instance.get_current_balance()
net_profit = current_bal - INITIAL_CAPITAL
df_ledger = pd.read_csv(TRADE_LOG_FILE) if os.path.exists(TRADE_LOG_FILE) else pd.DataFrame()

total_trades = len(df_ledger)
wins = len(df_ledger[df_ledger['PnL'] > 0]) if total_trades > 0 else 0
win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

col1, col2, col3, col4 = st.columns(4)
col1.metric("Account Balance", f"${current_bal:,.2f}", f"${net_profit:,.2f}")
col2.metric("Total Net Profit", f"${net_profit:,.2f}")
col3.metric("Win Rate", f"{win_rate:.1f}%", f"{wins}/{total_trades} Trades")
col4.metric("Active AI DNA Stretch", "Dynamic (1.7 - 2.8)")

st.markdown("---")

# Chart Section
st.subheader("📊 Live Market Strategy Charts & Bollinger Bands")
selected_pair = st.selectbox("Select Asset Pair:", PAIRS)

# Fetch data for chart display
df_chart = fetch_live_data(selected_pair, "15m", 100)
if not df_chart.empty:
    std_dev = bot_instance.dna_bb_std[selected_pair]
    df_chart['SMA20'] = df_chart['close'].rolling(20).mean()
    df_chart['STD'] = df_chart['close'].rolling(20).std()
    df_chart['BBL'] = df_chart['SMA20'] - (df_chart['STD'] * std_dev)
    df_chart['BBU'] = df_chart['SMA20'] + (df_chart['STD'] * std_dev)

    fig = go.Figure()
    fig.add_trace(go.Candlestick(x=df_chart.index, open=df_chart['open'], high=df_chart['high'], low=df_chart['low'], close=df_chart['close'], name="Candles"))
    fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart['BBU'], line=dict(color='gray', width=1), name="Upper BB"))
    fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart['BBL'], line=dict(color='gray', width=1), fill='tonexty', name="Lower BB (Dip Zone)"))
    fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart['SMA20'], line=dict(color='blue', width=1.5), name="SMA 20"))

    # Overlay active trade lines if any
    active_pos = bot_instance.positions[selected_pair]
    if active_pos:
        fig.add_hline(y=active_pos['entry'], line_dash="solid", line_color="orange", annotation_text="Entry Price")
        fig.add_hline(y=active_pos['tp'], line_dash="dash", line_color="green", annotation_text="Take Profit")
        fig.add_hline(y=active_pos['sl'], line_dash="dash", line_color="red", annotation_text="Stop Loss")

    fig.update_layout(height=500, template="plotly_dark", margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(fig, use_container_width=True)
else:
    st.warning("Fetching chart data from exchange...")

st.markdown("---")

# Trade Ledger Table
st.subheader("📜 Complete Trade Ledger History")
if not df_ledger.empty:
    st.dataframe(df_ledger.sort_index(ascending=False), use_container_width=True)
else:
    st.info("No closed trades recorded yet. The bot is scanning for high-probability pullbacks...")
# ==========================================
# 2. THE EVOLVING QUANTITATIVE STRATEGY
# ==========================================
class EvolvingPaperBot:
    def __init__(self, pairs, capital):
        self.pairs = pairs
        self.balance = capital
        self.positions = {pair: None for pair in pairs}
        # The AI's self-evolving sensitivity for each coin
        self.dna_bb_std = {pair: 2.0 for pair in pairs}
        
    def run_cycle(self):
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{current_time}] 👁️ AI SCANNING MARKET (Balance: ${self.balance:,.2f})", flush=True)
        print("-" * 55, flush=True)
        
        for pair in self.pairs:
            # Step A: Fetch 1-Hour and 15-Minute Data
            df_1h = fetch_live_data(pair, "1h", 100)
            time.sleep(1)
            df_15m = fetch_live_data(pair, "15m", 100)
            time.sleep(1)
            
            if df_1h.empty or df_15m.empty:
                continue
                
            # Step B: Gate 1 - Determine 1-Hour Trend Direction & Strength
            df_1h['EMA_20'] = df_1h['close'].ewm(span=20, adjust=False).mean()
            df_1h['EMA_50'] = df_1h['close'].ewm(span=50, adjust=False).mean()
            df_1h['ema_spread'] = (df_1h['EMA_20'] - df_1h['EMA_50']).abs() / df_1h['close'] * 100
            
            latest_1h = df_1h.iloc[-2]  # Last fully closed 1-Hour candle
            is_bull = (latest_1h['EMA_20'] > latest_1h['EMA_50']) and (latest_1h['ema_spread'] > 0.15)
            is_bear = (latest_1h['EMA_20'] < latest_1h['EMA_50']) and (latest_1h['ema_spread'] > 0.15)

            # Step C: Gate 2 - 15-Minute Bollinger Bands & ATR
            std_dev = self.dna_bb_std[pair]
            df_15m['SMA_20'] = df_15m['close'].rolling(window=20).mean()
            df_15m['STD_20'] = df_15m['close'].rolling(window=20).std()
            df_15m['BBL'] = df_15m['SMA_20'] - (df_15m['STD_20'] * std_dev)
            df_15m['BBU'] = df_15m['SMA_20'] + (df_15m['STD_20'] * std_dev)
            
            high_low = df_15m['high'] - df_15m['low']
            high_close = (df_15m['high'] - df_15m['close'].shift()).abs()
            low_close = (df_15m['low'] - df_15m['close'].shift()).abs()
            tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
            df_15m['ATR'] = tr.rolling(window=14).mean()
            
            live_candle = df_15m.iloc[-1]
            close = live_candle['close']
            atr = live_candle['ATR']
            
            # Step D: Manage Active Trades (Breakeven, Stop Loss, Take Profit)
            if self.positions[pair] is not None:
                pos = self.positions[pair]
                side = pos['side']
                ep = pos['entry']
                sl = pos['sl']
                tp = pos['tp']
                size = pos['size']
                
                # Move Stop Loss to Breakeven when in profit
                if side == 'LONG' and not pos['be_moved'] and live_candle['high'] >= pos['be_trigger']:
                    self.positions[pair]['sl'] = ep
                    self.positions[pair]['be_moved'] = True
                    print(f"🛡️ {pair}: Stop Loss moved to Breakeven ($0 risk)!", flush=True)
                elif side == 'SHORT' and not pos['be_moved'] and live_candle['low'] <= pos['be_trigger']:
                    self.positions[pair]['sl'] = ep
                    self.positions[pair]['be_moved'] = True
                    print(f"🛡️ {pair}: Stop Loss moved to Breakeven ($0 risk)!", flush=True)
                
                # Check for Exits
                if side == 'LONG':
                    pnl = (close - ep) * size
                    print(f"⏳ Holding {pair} LONG | Current: ${close:,.2f} | Open PnL: ${pnl:,.2f}", flush=True)
                    if live_candle['low'] <= sl:
                        self.close_trade(pair, sl, "Stop Loss", pnl > 0, pos['be_moved'])
                    elif live_candle['high'] >= tp:
                        self.close_trade(pair, tp, "Take Profit", True, pos['be_moved'])
                else:
                    pnl = (ep - close) * size
                    print(f"⏳ Holding {pair} SHORT | Current: ${close:,.2f} | Open PnL: ${pnl:,.2f}", flush=True)
                    if live_candle['high'] >= sl:
                        self.close_trade(pair, sl, "Stop Loss", pnl > 0, pos['be_moved'])
                    elif live_candle['low'] <= tp:
                        self.close_trade(pair, tp, "Take Profit", True, pos['be_moved'])
                continue

            # Step E: Scan for New Pullback Entries
            regime_label = 'BULL' if is_bull else 'BEAR' if is_bear else 'CHOP (NO TRADE)'
            print(f"🔎 {pair} | Regime: {regime_label} | BB Stretch: {std_dev:.2f} | Price: ${close:,.2f}", flush=True)
            
            risk_amt = self.balance * 0.01  # Strictly 1% risk per trade
            if is_bull and close <= live_candle['BBL']:
                sl = close - (atr * 2.0)
                tp = close + (atr * 4.0)
                size = risk_amt / (close - sl)
                self.open_trade(pair, 'LONG', close, sl, tp, size, close + (atr * 2.0))
            elif is_bear and close >= live_candle['BBU']:
                sl = close + (atr * 2.0)
                tp = close - (atr * 4.0)
                size = risk_amt / (sl - close)
                self.open_trade(pair, 'SHORT', close, sl, tp, size, close - (atr * 2.0))

    def open_trade(self, pair, side, ep, sl, tp, size, be_trigger):
        self.positions[pair] = {
            'side': side,
            'entry': ep,
            'sl': sl,
            'tp': tp,
            'size': size,
            'be_trigger': be_trigger,
            'be_moved': False
        }
        print(f"\n🚀 [TRADE TRIGGERED] {pair} {side} at ${ep:,.2f} | SL: ${sl:,.2f} | TP: ${tp:,.2f}", flush=True)

    def close_trade(self, pair, exit_price, reason, is_win, is_be):
        pos = self.positions[pair]
        gross_pnl = (exit_price - pos['entry']) * pos['size'] if pos['side'] == 'LONG' else (pos['entry'] - exit_price) * pos['size']
        net_pnl = gross_pnl - ((exit_price * pos['size']) * 0.002)  # Subtract simulated exchange fees
        self.balance += net_pnl
        print(f"\n✅ [TRADE CLOSED] {pair} at ${exit_price:,.2f} ({reason}) | Net PnL: ${net_pnl:,.2f}", flush=True)
        
        # Self-Evolution Engine
        if is_be:
            print(f"🧠 {pair} DNA unchanged (Breakeven trade).", flush=True)
        elif is_win:
            self.dna_bb_std[pair] = max(1.7, self.dna_bb_std[pair] - 0.1)
            print(f"🧠 AI EVOLVED: {pair} market clean. Lowered BB stretch to {self.dna_bb_std[pair]:.2f}", flush=True)
        else:
            self.dna_bb_std[pair] = min(2.8, self.dna_bb_std[pair] + 0.2)
            print(f"🧠 AI EVOLVED: {pair} market volatile. Raised BB stretch to {self.dna_bb_std[pair]:.2f}", flush=True)
            
        self.positions[pair] = None

# ==========================================
# 3. 24/7 CLOUD HOSTING ENGINE (FLASK + THREAD)
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Quantitative Trading Bot is running live 24/7!"

def run_bot_in_background():
    """Infinite loop executing the trading strategy."""
    time.sleep(5)  # Wait for web server to initialize
    print("🤖 STARTING LIVE PAPER TRADING ENGINE...", flush=True)
    bot = EvolvingPaperBot(PAIRS, INITIAL_CAPITAL)
    
    while True:
        try:
            bot.run_cycle()
            time.sleep(60)
        except Exception as e:
            print(f"⚠️ Loop error: {e}. Retrying in 60s...", flush=True)
            time.sleep(60)

# Starts the background trading process immediately when Render boots
threading.Thread(target=run_bot_in_background, daemon=True).start()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8080)
