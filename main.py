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
PAIRS = ["B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT", "B-XRP_USDT", "B-ADA_USDT"]
CAPITAL_INR = 100000.00
RISK_PER_TRADE_INR = 250.00
USDT_INR_RATE = 86.00 

MAKER_FEE = 0.00025  
TAKER_FEE = 0.00050  
SLIPPAGE_RATE = 0.0005 

AMTE_LEDGER = "amte_ledger.csv"
TW_LEDGER = "tw_ledger.csv"
TW_TUNED_LEDGER = "tw_tuned_ledger.csv"

# ==========================================
# CORE FUNCTIONS (FIXED API REQUEST)
# ==========================================
def fetch_live_data(pair, interval, limit=120):
    url = f"https://public.coindcx.com/market_data/candles?pair={pair}&interval={interval}&limit={limit}"
    # FIX: Added User-Agent to prevent CoinDCX from blocking the request (Fixes blank chart)
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

# ==========================================
# STRATEGY 1: AMTE BB PULLBACK ENGINE
# ==========================================
class AMTEBot:
    def __init__(self, pairs):
        self.pairs = pairs
        self.positions = {pair: {'status': 'NONE'} for pair in pairs}
        self.current_date = date.today()
        self.daily_trades = 0
        self.daily_pnl_inr = 0.0
        self.max_daily_trades = 10 
        self.max_daily_loss = -2000.00
        self.max_concurrent = 2

    def check_daily_reset(self):
        today = date.today()
        if today != self.current_date:
            self.current_date = today
            self.daily_trades = 0
            self.daily_pnl_inr = 0.0
            for p in self.positions:
                if self.positions[p]['status'] == 'PENDING_ENTRY':
                    self.positions[p] = {'status': 'NONE'}

    def process_cycle(self):
        self.check_daily_reset()
        kill_active = self.daily_trades >= self.max_daily_trades or self.daily_pnl_inr <= self.max_daily_loss
        active_count = sum(1 for p in self.positions.values() if p['status'] == 'ACTIVE')

        for pair in self.pairs:
            df_macro = fetch_live_data(pair, "1h", 100)
            time.sleep(0.5)
            df_micro = fetch_live_data(pair, "15m", 100)
            time.sleep(0.5)
            if df_macro.empty or df_micro.empty: continue

            df_macro['EMA_20'] = df_macro['close'].ewm(span=20, adjust=False).mean()
            df_macro['EMA_50'] = df_macro['close'].ewm(span=50, adjust=False).mean()
            df_macro['spread'] = (df_macro['EMA_20'] - df_macro['EMA_50']).abs() / df_macro['close'] * 100
            
            c_macro = df_macro.iloc[-2]
            is_bull = (c_macro['EMA_20'] > c_macro['EMA_50']) and (c_macro['spread'] > 0.05)
            is_bear = (c_macro['EMA_20'] < c_macro['EMA_50']) and (c_macro['spread'] > 0.05)

            df_micro['SMA20'] = df_micro['close'].rolling(20).mean()
            df_micro['STD20'] = df_micro['close'].rolling(20).std()
            df_micro['BBL'] = df_micro['SMA20'] - (df_micro['STD20'] * 1.5)
            df_micro['BBU'] = df_micro['SMA20'] + (df_micro['STD20'] * 1.5)
            tr = pd.concat([df_micro['high'] - df_micro['low'], (df_micro['high'] - df_micro['close'].shift()).abs(), (df_micro['low'] - df_micro['close'].shift()).abs()], axis=1).max(axis=1)
            df_micro['ATR'] = tr.rolling(14).mean()
            
            c_micro = df_micro.iloc[-2]
            live_price = df_micro.iloc[-1]['close']
            pos = self.positions[pair]

            if pos['status'] == 'ACTIVE':
                if (datetime.now() - pos['entry_time']).total_seconds() / 3600 >= 4:
                    self.close_trade(pair, live_price, "Timeout", "TAKER")
                    continue
                if pos['side'] == 'LONG':
                    if live_price <= pos['sl']: self.close_trade(pair, pos['sl'], "Stop Market", "TAKER")
                    elif live_price >= pos['tp']: self.close_trade(pair, pos['tp'], "Limit TP", "MAKER")
                else:
                    if live_price >= pos['sl']: self.close_trade(pair, pos['sl'], "Stop Market", "TAKER")
                    elif live_price <= pos['tp']: self.close_trade(pair, pos['tp'], "Limit TP", "MAKER")
                continue

            if pos['status'] == 'PENDING_ENTRY':
                if (pos['side'] == 'LONG' and not is_bull) or (pos['side'] == 'SHORT' and not is_bear):
                    self.positions[pair] = {'status': 'NONE'}
                    continue
                if pos['side'] == 'LONG' and df_micro.iloc[-1]['low'] <= pos['limit_price']: self.activate_trade(pair)
                elif pos['side'] == 'SHORT' and df_micro.iloc[-1]['high'] >= pos['limit_price']: self.activate_trade(pair)
                continue

            if kill_active or active_count >= self.max_concurrent: continue
            
            atr = c_micro['ATR']
            risk_usd = RISK_PER_TRADE_INR / USDT_INR_RATE
            if is_bull and live_price > c_micro['BBL']:
                lp = c_micro['BBL']
                self.positions[pair] = {'status': 'PENDING_ENTRY', 'side': 'LONG', 'limit_price': lp, 'sl': lp - (atr * 2.0), 'tp': lp + (atr * 4.0), 'size': risk_usd / (lp - (lp - (atr * 2.0)))}
            elif is_bear and live_price < c_micro['BBU']:
                lp = c_micro['BBU']
                self.positions[pair] = {'status': 'PENDING_ENTRY', 'side': 'SHORT', 'limit_price': lp, 'sl': lp + (atr * 2.0), 'tp': lp - (atr * 4.0), 'size': risk_usd / ((lp + (atr * 2.0)) - lp)}

    def activate_trade(self, pair):
        self.positions[pair].update({'status': 'ACTIVE', 'entry_time': datetime.now()})
        self.daily_trades += 1

    def close_trade(self, pair, exec_price, reason, order_type):
        pos = self.positions[pair]
        ep = pos['limit_price']
        exit_p = exec_price * (1 - (SLIPPAGE_RATE if order_type == "TAKER" else 0.0)) if pos['side'] == 'LONG' else exec_price * (1 + (SLIPPAGE_RATE if order_type == "TAKER" else 0.0))
        gross = (exit_p - ep) * pos['size'] if pos['side'] == 'LONG' else (ep - exit_p) * pos['size']
        fees = (ep * pos['size'] * MAKER_FEE) + (exit_p * pos['size'] * (MAKER_FEE if order_type == "MAKER" else TAKER_FEE))
        net_inr = (gross - fees) * USDT_INR_RATE
        self.daily_pnl_inr += net_inr
        
        log_trade(AMTE_LEDGER, {
            'Time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 'Pair': pair, 'Side': pos['side'],
            'Entry': round(ep, 2), 'Exit': round(exit_p, 2), 'Reason': reason, 'Net_PnL_INR': round(net_inr, 2)
        })
        self.positions[pair] = {'status': 'NONE'}

# ==========================================
# MASTER THREAD RUNNER
# ==========================================
class MasterEngine:
    def __init__(self):
        self.amte = AMTEBot(PAIRS)

    def loop(self):
        time.sleep(5)
        while True:
            try:
                self.amte.process_cycle()
                time.sleep(60)
            except:
                time.sleep(60)

@st.cache_resource
def start_master_engine():
    engine = MasterEngine()
    t = threading.Thread(target=engine.loop, daemon=True)
    t.start()
    return engine

# THIS LINE RESTARTS THE TRADING ENGINE
master = start_master_engine()

# ==========================================
# STREAMLIT USER INTERFACE (MOBILE OPTIMIZED)
# ==========================================
st.set_page_config(page_title="AMTE Terminal", layout="wide")
st.title("⚡ AMTE Live Trading Terminal")
st.caption("Engine is actively running in the background.")

df_amte_led = pd.read_csv(AMTE_LEDGER) if os.path.exists(AMTE_LEDGER) else pd.DataFrame()
net_amte = df_amte_led['Net_PnL_INR'].sum() if not df_amte_led.empty and 'Net_PnL_INR' in df_amte_led.columns else 0.0
active_amte = sum(1 for p in master.amte.positions.values() if p['status'] == 'ACTIVE')

# Mobile-friendly metrics
c1, c2 = st.columns(2)
c1.metric("Capital Balance", f"₹{(CAPITAL_INR + net_amte):,.2f}")
c2.metric("Market Exposure", f"{active_amte} Active Trades")

st.markdown("---")
pair_a = st.selectbox("Select Asset Pair to View Chart:", PAIRS)
df_chart_a = fetch_live_data(pair_a, "15m", 100)

if not df_chart_a.empty:
    df_chart_a['SMA20'] = df_chart_a['close'].rolling(20).mean()
    df_chart_a['STD20'] = df_chart_a['close'].rolling(20).std()
    df_chart_a['BBL'] = df_chart_a['SMA20'] - (df_chart_a['STD20'] * 1.5)
    df_chart_a['BBU'] = df_chart_a['SMA20'] + (df_chart_a['STD20'] * 1.5)

    fig_a = go.Figure()
    fig_a.add_trace(go.Candlestick(x=df_chart_a.index, open=df_chart_a['open'], high=df_chart_a['high'], low=df_chart_a['low'], close=df_chart_a['close'], name="15m Candles"))
    fig_a.add_trace(go.Scatter(x=df_chart_a.index, y=df_chart_a['BBU'], line=dict(color='rgba(150, 150, 150, 0.5)', width=1), name="Upper BB"))
    fig_a.add_trace(go.Scatter(x=df_chart_a.index, y=df_chart_a['BBL'], line=dict(color='rgba(150, 150, 150, 0.5)', width=1), fill='tonexty', fillcolor='rgba(100, 100, 255, 0.05)', name="Lower BB"))
    
    pos_a = master.amte.positions[pair_a]
    if pos_a['status'] == 'PENDING_ENTRY':
        fig_a.add_hline(y=pos_a['limit_price'], line_dash="dot", line_color="#BDBDBD")
    elif pos_a['status'] == 'ACTIVE':
        fig_a.add_hline(y=pos_a['limit_price'], line_dash="solid", line_color="#FF9800")
        fig_a.add_hline(y=pos_a['tp'], line_dash="dash", line_color="#00E676")
        fig_a.add_hline(y=pos_a['sl'], line_dash="dash", line_color="#FF5252")

    fig_a.update_layout(height=450, template="plotly_dark", xaxis_rangeslider_visible=False, margin=dict(l=0, r=0, t=10, b=10))
    st.plotly_chart(fig_a, use_container_width=True)
else:
    st.error("Failed to load chart data from CoinDCX API.")

if not df_amte_led.empty:
    st.subheader("Recent Paper Trades")
    st.dataframe(df_amte_led.sort_index(ascending=False).head(10), use_container_width=True)
