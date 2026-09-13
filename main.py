import os
import time
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
# AMTE SYSTEM SPECIFICATION
# ==========================================
PAIRS = ["B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT"]
CAPITAL_INR = 100000.00
RISK_PER_TRADE_INR = 250.00  # 0.25% of ₹1 Lakh
MAX_DAILY_TRADES = 5
MAX_DAILY_LOSS_INR = -1000.00
USDT_INR_RATE = 86.00 
FEE_RATE = 0.001       
SLIPPAGE_RATE = 0.0005 
TRADE_LOG_FILE = "amte_ledger.csv"

# --- TELEGRAM SETTINGS ---
TELEGRAM_BOT_TOKEN = "PASTE_YOUR_BOT_TOKEN_HERE" 
TELEGRAM_CHAT_ID = "PASTE_YOUR_CHAT_ID_HERE"

def send_telegram_alert(message):
    """Sends a push notification to your phone."""
    if TELEGRAM_BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=5)
    except:
        pass

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
# AMTE EXECUTION ENGINE
# ==========================================
class AMTEBot:
    def __init__(self, pairs):
        self.pairs = pairs
        self.positions = {pair: None for pair in pairs}
        self.current_date = date.today()
        self.daily_trades = 0
        self.daily_pnl_inr = 0.0
        
        if not os.path.exists(TRADE_LOG_FILE):
            pd.DataFrame(columns=[
                'Time', 'Pair', 'Side', 'Entry', 'Exit', 'Reason', 
                'Gross_USD', 'Fees_USD', 'Slippage_USD', 'Net_PnL_INR'
            ]).to_csv(TRADE_LOG_FILE, index=False)

    def check_daily_reset(self):
        today = date.today()
        if today != self.current_date:
            self.current_date = today
            self.daily_trades = 0
            self.daily_pnl_inr = 0.0
            send_telegram_alert("🔄 <b>AMTE Pipeline Reset</b>\nDaily limits cleared for the new trading day.")

    def run_loop(self):
        time.sleep(5)
        send_telegram_alert("🚀 <b>AMTE System Live</b>\nScanning markets with ₹1 Lakh baseline.")
        
        while True:
            try:
                self.check_daily_reset()
                
                kill_switch_active = False
                if self.daily_trades >= MAX_DAILY_TRADES:
                    kill_switch_active = True
                if self.daily_pnl_inr <= MAX_DAILY_LOSS_INR:
                    kill_switch_active = True

                for pair in self.pairs:
                    df_1h = fetch_live_data(pair, "1h", 100)
                    time.sleep(1)
                    df_15m = fetch_live_data(pair, "15m", 100)
                    time.sleep(1)
                    
                    if df_1h.empty or df_15m.empty: continue
                    
                    # 1H Closed Regime Filter
                    df_1h['EMA_20'] = df_1h['close'].ewm(span=20, adjust=False).mean()
                    df_1h['EMA_50'] = df_1h['close'].ewm(span=50, adjust=False).mean()
                    df_1h['ema_spread'] = (df_1h['EMA_20'] - df_1h['EMA_50']).abs() / df_1h['close'] * 100
                    
                    closed_1h = df_1h.iloc[-2]
                    is_bull = (closed_1h['EMA_20'] > closed_1h['EMA_50']) and (closed_1h['ema_spread'] > 0.15)
                    is_bear = (closed_1h['EMA_20'] < closed_1h['EMA_50']) and (closed_1h['ema_spread'] > 0.15)

                    # 15m Indicators
                    df_15m['SMA_20'] = df_15m['close'].rolling(window=20).mean()
                    df_15m['STD_20'] = df_15m['close'].rolling(window=20).std()
                    df_15m['BBL'] = df_15m['SMA_20'] - (df_15m['STD_20'] * 2.0)
                    df_15m['BBU'] = df_15m['SMA_20'] + (df_15m['STD_20'] * 2.0)
                    
                    tr = pd.concat([df_15m['high'] - df_15m['low'], 
                                    (df_15m['high'] - df_15m['close'].shift()).abs(), 
                                    (df_15m['low'] - df_15m['close'].shift()).abs()], axis=1).max(axis=1)
                    df_15m['ATR'] = tr.rolling(window=14).mean()
                    
                    closed_15m = df_15m.iloc[-2]
                    live_price = df_15m.iloc[-1]['close']
                    
                    # Deterministic Trade Management
                    if self.positions[pair] is not None:
                        pos = self.positions[pair]
                        side, sl, tp = pos['side'], pos['sl'], pos['tp']
                        
                        if side == 'LONG':
                            if live_price <= sl: self.close_trade(pair, live_price, "Stop Loss")
                            elif live_price >= tp: self.close_trade(pair, live_price, "Take Profit")
                        else:
                            if live_price >= sl: self.close_trade(pair, live_price, "Stop Loss")
                            elif live_price <= tp: self.close_trade(pair, live_price, "Take Profit")
                        continue

                    if kill_switch_active:
                        continue

                    risk_usd = RISK_PER_TRADE_INR / USDT_INR_RATE
                    atr = closed_15m['ATR']
                    
                    if is_bull and closed_15m['close'] <= closed_15m['BBL']:
                        sl, tp = live_price - (atr * 2.0), live_price + (atr * 4.0)
                        size = risk_usd / (live_price - sl)
                        exec_price = live_price * (1 + SLIPPAGE_RATE)
                        self.open_trade(pair, 'LONG', exec_price, sl, tp, size)
                        
                    elif is_bear and closed_15m['close'] >= closed_15m['BBU']:
                        sl, tp = live_price + (atr * 2.0), live_price - (atr * 4.0)
                        size = risk_usd / (sl - live_price)
                        exec_price = live_price * (1 - SLIPPAGE_RATE)
                        self.open_trade(pair, 'SHORT', exec_price, sl, tp, size)
                        
                time.sleep(60)
            except Exception as e:
                time.sleep(60)

    def open_trade(self, pair, side, ep, sl, tp, size):
        self.positions[pair] = {'side': side, 'entry': ep, 'sl': sl, 'tp': tp, 'size': size}
        self.daily_trades += 1
        
        msg = f"🟢 <b>NEW TRADE ENTERED</b>\nPair: {pair}\nSide: {side}\nEntry: ${ep:,.2f}\nSL: ${sl:,.2f}\nTP: ${tp:,.2f}"
        send_telegram_alert(msg)
        
        if self.daily_trades >= MAX_DAILY_TRADES:
            send_telegram_alert("🔴 <b>KILL SWITCH TRIGGERED</b>\nMax daily trades (5) reached. Bot sleeping until midnight.")

    def close_trade(self, pair, exit_price, reason):
        pos = self.positions[pair]
        actual_exit = exit_price * (1 - SLIPPAGE_RATE) if pos['side'] == 'LONG' else exit_price * (1 + SLIPPAGE_RATE)
        
        gross_usd = (actual_exit - pos['entry']) * pos['size'] if pos['side'] == 'LONG' else (pos['entry'] - actual_exit) * pos['size']
        fees_usd = ((pos['entry'] * pos['size']) * FEE_RATE) + ((actual_exit * pos['size']) * FEE_RATE)
        net_usd = gross_usd - fees_usd
        net_inr = net_usd * USDT_INR_RATE
        
        self.daily_pnl_inr += net_inr
        
        log_trade_to_csv({
            'Time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'Pair': pair, 'Side': pos['side'],
            'Entry': round(pos['entry'], 2), 'Exit': round(actual_exit, 2),
            'Reason': reason, 'Gross_USD': round(gross_usd, 2),
            'Fees_USD': round(fees_usd, 2), 
            'Slippage_USD': round(abs(exit_price - actual_exit) * pos['size'], 2),
            'Net_PnL_INR': round(net_inr, 2)
        })
        self.positions[pair] = None
        
        icon = "✅" if net_inr > 0 else "❌"
        msg = f"{icon} <b>TRADE CLOSED</b>\nPair: {pair}\nReason: {reason}\nExit: ${actual_exit:,.2f}\nNet PnL: ₹{net_inr:,.2f}\nDaily PnL: ₹{self.daily_pnl_inr:,.2f}"
        send_telegram_alert(msg)
        
        if self.daily_pnl_inr <= MAX_DAILY_LOSS_INR:
            send_telegram_alert(f"🛑 <b>LOSS LIMIT HIT</b>\nDaily loss of ₹{self.daily_pnl_inr:,.2f} exceeds limit. Trading halted until midnight.")

@st.cache_resource
def start_background_bot():
    bot = AMTEBot(PAIRS)
    t = threading.Thread(target=bot.run_loop, daemon=True)
    t.start()
    return bot

bot_instance = start_background_bot()

# ==========================================
# STREAMLIT DASHBOARD UI
# ==========================================
st.set_page_config(page_title="AMTE Quantitative Terminal", layout="wide")
st.title("⚡ AMTE Experimental Pipeline (₹1 Lakh Baseline)")

df_ledger = pd.read_csv(TRADE_LOG_FILE) if os.path.exists(TRADE_LOG_FILE) else pd.DataFrame()
total_net_inr = df_ledger['Net_PnL_INR'].sum() if not df_ledger.empty and 'Net_PnL_INR' in df_ledger.columns else 0.0
current_bal_inr = CAPITAL_INR + total_net_inr

kill_switch_status = "🟢 ACTIVE"
if bot_instance.daily_trades >= MAX_DAILY_TRADES or bot_instance.daily_pnl_inr <= MAX_DAILY_LOSS_INR:
    kill_switch_status = "🔴 TRIGGERED (Sleeping)"

col1, col2, col3, col4 = st.columns(4)
col1.metric("Account Balance (INR)", f"₹{current_bal_inr:,.2f}", f"₹{total_net_inr:,.2f}")
col2.metric("Today's Trades", f"{bot_instance.daily_trades} / {MAX_DAILY_TRADES}")
col3.metric("Today's PnL", f"₹{bot_instance.daily_pnl_inr:,.2f}")
col4.metric("Daily Kill Switch", kill_switch_status)

st.markdown("---")

# ==========================================
# INTERACTIVE STRATEGY CHARTS & OVERLAYS
# ==========================================
st.subheader("📊 Live Strategy Charts & Bollinger Bands")
selected_pair = st.selectbox("Select Asset Pair:", PAIRS)

df_chart = fetch_live_data(selected_pair, "15m", 100)
if not df_chart.empty:
    df_chart['SMA20'] = df_chart['close'].rolling(20).mean()
    df_chart['STD'] = df_chart['close'].rolling(20).std()
    df_chart['BBL'] = df_chart['SMA20'] - (df_chart['STD'] * 2.0)
    df_chart['BBU'] = df_chart['SMA20'] + (df_chart['STD'] * 2.0)

    fig = go.Figure()
    
    # 1. Candlestick Price Action
    fig.add_trace(go.Candlestick(
        x=df_chart.index,
        open=df_chart['open'], high=df_chart['high'],
        low=df_chart['low'], close=df_chart['close'],
        name="15m Candles"
    ))
    
    # 2. Bollinger Band Bands & Moving Average
    fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart['BBU'], line=dict(color='rgba(150, 150, 150, 0.5)', width=1), name="Upper BB"))
    fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart['BBL'], line=dict(color='rgba(150, 150, 150, 0.5)', width=1), fill='tonexty', fillcolor='rgba(100, 100, 255, 0.05)', name="Lower BB"))
    fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart['SMA20'], line=dict(color='#2962FF', width=1.5), name="SMA 20"))

    # 3. Dynamic Trade Overlay (Entry, Stop Loss, Take Profit)
    active_pos = bot_instance.positions[selected_pair]
    if active_pos:
        fig.add_hline(y=active_pos['entry'], line_dash="solid", line_color="#FF9800", annotation_text=f"Entry: ${active_pos['entry']:,.2f}")
        fig.add_hline(y=active_pos['tp'], line_dash="dash", line_color="#00E676", annotation_text=f"Take Profit: ${active_pos['tp']:,.2f}")
        fig.add_hline(y=active_pos['sl'], line_dash="dash", line_color="#FF5252", annotation_text=f"Stop Loss: ${active_pos['sl']:,.2f}")

    fig.update_layout(
        height=520,
        template="plotly_dark",
        xaxis_rangeslider_visible=False,
        margin=dict(l=15, r=15, t=20, b=15)
    )
    st.plotly_chart(fig, use_container_width=True)
else:
    st.warning("Fetching real-time market data from CoinDCX...")

st.markdown("---")

# ==========================================
# TRADE LEDGER TABLE
# ==========================================
st.subheader("📜 AMTE Trade Ledger (Cost-Aware)")
if not df_ledger.empty:
    st.dataframe(df_ledger.sort_index(ascending=False), use_container_width=True)
else:
    st.info("No trades executed yet. The AMTE pipeline is strictly monitoring closed candles.")
