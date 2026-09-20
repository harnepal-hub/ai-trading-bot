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

TELEGRAM_BOT_TOKEN = "PASTE_YOUR_BOT_TOKEN_HERE" 
TELEGRAM_CHAT_ID = "PASTE_YOUR_CHAT_ID_HERE"

def send_telegram_alert(message):
    if TELEGRAM_BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE": return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try: requests.post(url, json=payload, timeout=5)
    except: pass

def fetch_live_data(pair, interval, limit=120):
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

def log_trade(filename, trade_data):
    df_new = pd.DataFrame([trade_data])
    if os.path.exists(filename):
        df_new.to_csv(filename, mode='a', header=False, index=False)
    else:
        df_new.to_csv(filename, mode='w', header=True, index=False)

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
    
    last_p_high = np.nan
    last_p_low = np.nan
    last_q_high = np.nan
    last_q_low = np.nan
    
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
# STRATEGY 1: AMTE BB PULLBACK ENGINE
# ==========================================
class AMTEBot:
    def __init__(self, pairs):
        self.pairs = pairs
        self.positions = {pair: {'status': 'NONE'} for pair in pairs}
        self.current_date = date.today()
        self.daily_trades = 0
        self.daily_pnl_inr = 0.0
        self.max_daily_trades = 10  # Capped at 10 trades per day
        self.max_daily_loss = -1000.00
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
            tr = pd.concat([df_micro['high'] - df_micro['low'], 
                            (df_micro['high'] - df_micro['close'].shift()).abs(), 
                            (df_micro['low'] - df_micro['close'].shift()).abs()], axis=1).max(axis=1)
            df_micro['ATR'] = tr.rolling(14).mean()
            
            c_micro = df_micro.iloc[-2]
            live_price = df_micro.iloc[-1]['close']
            live_low = df_micro.iloc[-1]['low']
            live_high = df_micro.iloc[-1]['high']
            pos = self.positions[pair]

            if pos['status'] == 'ACTIVE':
                if (datetime.now() - pos['entry_time']).total_seconds() / 3600 >= 4:
                    self.close_trade(pair, live_price, "Timeout", "TAKER")
                    continue
                if not pos['be_moved']:
                    if (pos['side'] == 'LONG' and live_price >= pos['be_trig']) or \
                       (pos['side'] == 'SHORT' and live_price <= pos['be_trig']):
                        self.positions[pair]['sl'] = pos['be_sl']
                        self.positions[pair]['be_moved'] = True
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
                if pos['side'] == 'LONG' and live_low <= pos['limit_price']:
                    self.activate_trade(pair)
                elif pos['side'] == 'SHORT' and live_high >= pos['limit_price']:
                    self.activate_trade(pair)
                continue

            if kill_active or active_count >= self.max_concurrent: continue
            
            risk_usd = RISK_PER_TRADE_INR / USDT_INR_RATE
            atr = c_micro['ATR']
            if is_bull and live_price > c_micro['BBL']:
                lp = c_micro['BBL']
                sl, tp = lp - (atr * 2.0), lp + (atr * 4.0)
                size = risk_usd / (lp - sl)
                self.place_limit(pair, 'LONG', lp, sl, tp, size, atr)
            elif is_bear and live_price < c_micro['BBU']:
                lp = c_micro['BBU']
                sl, tp = lp + (atr * 2.0), lp - (atr * 4.0)
                size = risk_usd / (sl - lp)
                self.place_limit(pair, 'SHORT', lp, sl, tp, size, atr)

    def place_limit(self, pair, side, lp, sl, tp, size, atr):
        be_trig = lp + (atr * 2.0) if side == 'LONG' else lp - (atr * 2.0)
        cost_buf = lp * ((MAKER_FEE * 2) + TAKER_FEE)
        be_sl = lp + cost_buf if side == 'LONG' else lp - cost_buf
        self.positions[pair] = {
            'status': 'PENDING_ENTRY', 'side': side, 'limit_price': lp,
            'sl': sl, 'tp': tp, 'size': size, 'be_trig': be_trig, 'be_sl': be_sl, 'be_moved': False
        }

    def activate_trade(self, pair):
        self.positions[pair]['status'] = 'ACTIVE'
        self.positions[pair]['entry_time'] = datetime.now()
        self.daily_trades += 1
        pos = self.positions[pair]
        send_telegram_alert(f"🟢 <b>[AMTE] ORDER FILLED</b>\nPair: {pair}\nSide: {pos['side']}\nPrice: ${pos['limit_price']:,.2f}")

    def close_trade(self, pair, exec_price, reason, order_type):
        pos = self.positions[pair]
        ep = pos['limit_price']
        slip = SLIPPAGE_RATE if order_type == "TAKER" else 0.0
        exit_p = exec_price * (1 - slip) if pos['side'] == 'LONG' else exec_price * (1 + slip)
        gross = (exit_p - ep) * pos['size'] if pos['side'] == 'LONG' else (ep - exit_p) * pos['size']
        fees = (ep * pos['size'] * MAKER_FEE) + (exit_p * pos['size'] * (MAKER_FEE if order_type == "MAKER" else TAKER_FEE))
        net_inr = (gross - fees) * USDT_INR_RATE
        self.daily_pnl_inr += net_inr
        
        log_trade(AMTE_LEDGER, {
            'Time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 'Pair': pair, 'Side': pos['side'],
            'Entry': round(ep, 2), 'Exit': round(exit_p, 2), 'Reason': reason,
            'Gross_USD': round(gross, 2), 'Fees_USD': round(fees, 2),
            'Slippage_USD': round(abs(exec_price - exit_p) * pos['size'], 2), 'Net_PnL_INR': round(net_inr, 2)
        })
        self.positions[pair] = {'status': 'NONE'}
        send_telegram_alert(f"🔔 <b>[AMTE] CLOSED</b>\nPair: {pair}\nReason: {reason}\nNet: ₹{net_inr:,.2f}")

# ==========================================
# STRATEGY 2: TW ALL-IN-ONE ENGINE
# ==========================================
class TWAllInOneBot:
    def __init__(self, pairs):
        self.pairs = pairs
        self.positions = {pair: {'status': 'NONE'} for pair in pairs}
        self.current_date = date.today()
        self.daily_trades = 0
        self.daily_pnl_inr = 0.0
        self.max_daily_trades = 10 
        self.max_daily_loss = -2000.00
        self.max_concurrent = 3

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
            df = fetch_live_data(pair, "15m", 120)
            time.sleep(0.5)
            if df.empty or len(df) < 105: continue

            df['EMA100'] = df['close'].ewm(span=100, adjust=False).mean()
            df['MHULL'] = calculate_ehma(df['close'], length=16)
            df['SHULL'] = df['MHULL'].shift(2)

            tr = pd.concat([df['high'] - df['low'], 
                            (df['high'] - df['close'].shift()).abs(), 
                            (df['low'] - df['close'].shift()).abs()], axis=1).max(axis=1)
            df['ATR'] = tr.rolling(14).mean()

            c_prev = df.iloc[-3]
            c_curr = df.iloc[-2]
            live_candle = df.iloc[-1]
            live_price = live_candle['close']

            buy_signal = (c_prev['SHULL'] >= c_prev['MHULL']) and (c_curr['SHULL'] < c_curr['MHULL']) and (c_curr['close'] > c_curr['EMA100'])
            sell_signal = (c_prev['SHULL'] <= c_prev['MHULL']) and (c_curr['SHULL'] > c_curr['MHULL']) and (c_curr['close'] < c_curr['EMA100'])

            pos = self.positions[pair]

            if pos['status'] == 'ACTIVE':
                if (datetime.now() - pos['entry_time']).total_seconds() / 3600 >= 6:
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
                if live_candle['low'] <= pos['limit_price'] if pos['side'] == 'LONG' else live_candle['high'] >= pos['limit_price']:
                    self.activate_trade(pair)
                continue

            if kill_active or active_count >= self.max_concurrent: continue

            p_high, p_low, q_high, q_low = calculate_pivots(df)
            atr = c_curr['ATR']
            risk_usd = RISK_PER_TRADE_INR / USDT_INR_RATE

            if buy_signal:
                limit_p = live_price
                sl = q_low if (not np.isnan(q_low) and q_low < limit_p) else (limit_p - atr * 1.8)
                tp = p_high if (not np.isnan(p_high) and p_high > limit_p) else (limit_p + atr * 3.6)
                size = risk_usd / max(0.0001, (limit_p - sl))
                self.positions[pair] = {'status': 'PENDING_ENTRY', 'side': 'LONG', 'limit_price': limit_p, 'sl': sl, 'tp': tp, 'size': size}
                send_telegram_alert(f"🎯 <b>[TW All-in-One] BUY SIGNAL</b>\nPair: {pair}\nPrice: ${limit_p:,.2f}")

            elif sell_signal:
                limit_p = live_price
                sl = q_high if (not np.isnan(q_high) and q_high > limit_p) else (limit_p + atr * 1.8)
                tp = p_low if (not np.isnan(p_low) and p_low < limit_p) else (limit_p - atr * 3.6)
                size = risk_usd / max(0.0001, (sl - limit_p))
                self.positions[pair] = {'status': 'PENDING_ENTRY', 'side': 'SHORT', 'limit_price': limit_p, 'sl': sl, 'tp': tp, 'size': size}
                send_telegram_alert(f"🎯 <b>[TW All-in-One] SELL SIGNAL</b>\nPair: {pair}\nPrice: ${limit_p:,.2f}")

    def activate_trade(self, pair):
        self.positions[pair]['status'] = 'ACTIVE'
        self.positions[pair]['entry_time'] = datetime.now()
        self.daily_trades += 1
        pos = self.positions[pair]
        send_telegram_alert(f"🟢 <b>[TW All-In-One] FILLED</b>\nPair: {pair}\nSide: {pos['side']} at ${pos['limit_price']:,.2f}")

    def close_trade(self, pair, exec_price, reason, order_type):
        pos = self.positions[pair]
        ep = pos['limit_price']
        slip = SLIPPAGE_RATE if order_type == "TAKER" else 0.0
        exit_p = exec_price * (1 - slip) if pos['side'] == 'LONG' else exec_price * (1 + slip)
        gross = (exit_p - ep) * pos['size'] if pos['side'] == 'LONG' else (ep - exit_p) * pos['size']
        fees = (ep * pos['size'] * MAKER_FEE) + (exit_p * pos['size'] * (MAKER_FEE if order_type == "MAKER" else TAKER_FEE))
        net_inr = (gross - fees) * USDT_INR_RATE
        self.daily_pnl_inr += net_inr

        log_trade(TW_LEDGER, {
            'Time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 'Pair': pair, 'Side': pos['side'],
            'Entry': round(ep, 2), 'Exit': round(exit_p, 2), 'Reason': reason,
            'Gross_USD': round(gross, 2), 'Fees_USD': round(fees, 2),
            'Slippage_USD': round(abs(exec_price - exit_p) * pos['size'], 2), 'Net_PnL_INR': round(net_inr, 2)
        })
        self.positions[pair] = {'status': 'NONE'}
        send_telegram_alert(f"🔔 <b>[TW All-In-One] CLOSED</b>\nPair: {pair}\nReason: {reason}\nNet: ₹{net_inr:,.2f}")

# ==========================================
# MASTER THREAD RUNNER
# ==========================================
class MasterEngine:
    def __init__(self):
        self.amte = AMTEBot(PAIRS)
        self.tw = TWAllInOneBot(PAIRS)

    def loop(self):
        time.sleep(5)
        send_telegram_alert("🚀 <b>Dual Quantitative Pipeline Live</b>\nTab 1: AMTE BB Pullback (10 Trades)\nTab 2: TW All-In-One (10 Trades)")
        while True:
            try:
                self.amte.process_cycle()
                self.tw.process_cycle()
                time.sleep(60)
            except:
                time.sleep(60)

@st.cache_resource
def start_master_engine():
    engine = MasterEngine()
    t = threading.Thread(target=engine.loop, daemon=True)
    t.start()
    return engine

master = start_master_engine()

# ==========================================
# STREAMLIT USER INTERFACE WITH TABS
# ==========================================
st.set_page_config(page_title="AMTE Quantitative Terminal", layout="wide")
st.title("⚡ AMTE Quantitative Multi-Model Terminal")

tab1, tab2 = st.tabs(["⚡ Strategy A: AMTE BB Pullback (10 Trades/Day)", "🎯 Strategy B: TW All-In-One (10 Trades/Day)"])

# ------------------------------------------
# TAB 1: AMTE BB PULLBACK
# ------------------------------------------
with tab1:
    df_amte_led = pd.read_csv(AMTE_LEDGER) if os.path.exists(AMTE_LEDGER) else pd.DataFrame()
    net_amte = df_amte_led['Net_PnL_INR'].sum() if not df_amte_led.empty and 'Net_PnL_INR' in df_amte_led.columns else 0.0
    active_amte = sum(1 for p in master.amte.positions.values() if p['status'] == 'ACTIVE')
    pending_amte = sum(1 for p in master.amte.positions.values() if p['status'] == 'PENDING_ENTRY')

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Capital Balance", f"₹{(CAPITAL_INR + net_amte):,.2f}", f"₹{net_amte:,.2f}")
    c2.metric("Market Exposure", f"{active_amte} Active / {pending_amte} Pending")
    c3.metric("Today's Trades", f"{master.amte.daily_trades} / {master.amte.max_daily_trades}")
    c4.metric("Today's PnL", f"₹{master.amte.daily_pnl_inr:,.2f}")

    st.markdown("---")
    st.subheader("📊 15m Bollinger Band Pullback Routing")
    pair_a = st.selectbox("Select Asset Pair (Strategy A):", PAIRS, key="pair_a")
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
            fig_a.add_hline(y=pos_a['limit_price'], line_dash="dot", line_color="#BDBDBD", annotation_text=f"Resting Limit: ${pos_a['limit_price']:,.2f}")
        elif pos_a['status'] == 'ACTIVE':
            fig_a.add_hline(y=pos_a['limit_price'], line_dash="solid", line_color="#FF9800", annotation_text=f"Entry: ${pos_a['limit_price']:,.2f}")
            fig_a.add_hline(y=pos_a['tp'], line_dash="dash", line_color="#00E676", annotation_text=f"TP: ${pos_a['tp']:,.2f}")
            fig_a.add_hline(y=pos_a['sl'], line_dash="dash", line_color="#FF5252", annotation_text=f"SL: ${pos_a['sl']:,.2f}")

        fig_a.update_layout(height=500, template="plotly_dark", xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig_a, use_container_width=True)

    st.subheader("📜 AMTE Trade Ledger")
    if not df_amte_led.empty:
        csv_a = df_amte_led.to_csv(index=False).encode('utf-8')
        st.download_button(label="📥 Download AMTE Ledger", data=csv_a, file_name='amte_ledger.csv', mime='text/csv')
        st.dataframe(df_amte_led.sort_index(ascending=False), use_container_width=True)
    else:
        st.info("No trades executed yet under Strategy A.")

# ------------------------------------------
# TAB 2: TW ALL-IN-ONE REFINEMENT ENGINE
# ------------------------------------------
with tab2:
    df_tw_led = pd.read_csv(TW_LEDGER) if os.path.exists(TW_LEDGER) else pd.DataFrame()
    net_tw = df_tw_led['Net_PnL_INR'].sum() if not df_tw_led.empty and 'Net_PnL_INR' in df_tw_led.columns else 0.0
    active_tw = sum(1 for p in master.tw.positions.values() if p['status'] == 'ACTIVE')
    pending_tw = sum(1 for p in master.tw.positions.values() if p['status'] == 'PENDING_ENTRY')

    t1, t2, t3, t4 = st.columns(4)
    t1.metric("Capital Balance", f"₹{(CAPITAL_INR + net_tw):,.2f}", f"₹{net_tw:,.2f}")
    t2.metric("Market Exposure", f"{active_tw} Active / {pending_tw} Pending")
    t3.metric("Today's Trades", f"{master.tw.daily_trades} / {master.tw.max_daily_trades}")
    t4.metric("Today's PnL", f"₹{master.tw.daily_pnl_inr:,.2f}")

    st.markdown("---")
    st.subheader("📊 TW All-In-One Indicator Overlay")
    pair_b = st.selectbox("Select Asset Pair (Strategy B):", PAIRS, key="pair_b")
    df_chart_b = fetch_live_data(pair_b, "15m", 120)

    if not df_chart_b.empty and len(df_chart_b) >= 105:
        df_chart_b['EMA100'] = df_chart_b['close'].ewm(span=100, adjust=False).mean()
        df_chart_b['MHULL'] = calculate_ehma(df_chart_b['close'], 16)
        df_chart_b['SHULL'] = df_chart_b['MHULL'].shift(2)
        p_high, p_low, q_high, q_low = calculate_pivots(df_chart_b)

        fig_b = go.Figure()
        fig_b.add_trace(go.Candlestick(x=df_chart_b.index, open=df_chart_b['open'], high=df_chart_b['high'], low=df_chart_b['low'], close=df_chart_b['close'], name="15m Candles"))
        fig_b.add_trace(go.Scatter(x=df_chart_b.index, y=df_chart_b['MHULL'], line=dict(color='#0018F3', width=2), name="MHULL"))
        fig_b.add_trace(go.Scatter(x=df_chart_b.index, y=df_chart_b['SHULL'], line=dict(color='#FF4B4B', width=1.5, dash='dot'), name="SHULL"))
        fig_b.add_trace(go.Scatter(x=df_chart_b.index, y=df_chart_b['EMA100'], line=dict(color='#9C27B0', width=2), name="EMA 100"))

        if not np.isnan(p_high):
            fig_b.add_hline(y=p_high, line_dash="dash", line_color="#00E676", annotation_text=f"Target: ${p_high:,.2f}")
        if not np.isnan(q_low):
            fig_b.add_hline(y=q_low, line_dash="dash", line_color="#FF5252", annotation_text=f"Support: ${q_low:,.2f}")

        pos_b = master.tw.positions[pair_b]
        if pos_b['status'] == 'PENDING_ENTRY':
            fig_b.add_hline(y=pos_b['limit_price'], line_dash="dot", line_color="#BDBDBD", annotation_text=f"Limit Order: ${pos_b['limit_price']:,.2f}")
        elif pos_b['status'] == 'ACTIVE':
            fig_b.add_hline(y=pos_b['limit_price'], line_dash="solid", line_color="#FF9800", annotation_text=f"Filled: ${pos_b['limit_price']:,.2f}")
            fig_b.add_hline(y=pos_b['tp'], line_dash="dash", line_color="#00E676", annotation_text=f"TP: ${pos_b['tp']:,.2f}")
            fig_b.add_hline(y=pos_b['sl'], line_dash="dash", line_color="#FF5252", annotation_text=f"SL: ${pos_b['sl']:,.2f}")

        fig_b.update_layout(height=500, template="plotly_dark", xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig_b, use_container_width=True)

    st.subheader("📜 TW All-In-One Trade Ledger")
    if not df_tw_led.empty:
        csv_b = df_tw_led.to_csv(index=False).encode('utf-8')
        st.download_button(label="📥 Download TW Ledger", data=csv_b, file_name='tw_ledger.csv', mime='text/csv')
        st.dataframe(df_tw_led.sort_index(ascending=False), use_container_width=True)
    else:
        st.info("No trades executed yet under TW All-In-One.")
