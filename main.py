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
# AMTE SYSTEM SPECIFICATION v2.0 (Swing)
# ==========================================
PAIRS = ["B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT", "B-XRP_USDT", "B-ADA_USDT"]
CAPITAL_INR = 100000.00
RISK_PER_TRADE_INR = 250.00  # 0.25% of ₹1 Lakh
MAX_DAILY_TRADES = 5
MAX_DAILY_LOSS_INR = -1000.00
MAX_CONCURRENT_POSITIONS = 2 # Aggregate Risk Cap
USDT_INR_RATE = 86.00 
FEE_RATE = 0.001       
SLIPPAGE_RATE = 0.0005 
TRADE_LOG_FILE = "amte_ledger.csv"

# --- TELEGRAM SETTINGS ---
TELEGRAM_BOT_TOKEN = "PASTE_YOUR_BOT_TOKEN_HERE" 
TELEGRAM_CHAT_ID = "PASTE_YOUR_CHAT_ID_HERE"

def send_telegram_alert(message):
    if TELEGRAM_BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE": return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try: requests.post(url, json=payload, timeout=5)
    except: pass

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
            send_telegram_alert("🔄 <b>AMTE Pipeline Reset</b>\nDaily limits cleared.")

    def run_loop(self):
        time.sleep(5)
        send_telegram_alert("🚀 <b>AMTE Swing Engine Live</b>\nScanning 4H/1H regimes.")
        
        while True:
            try:
                self.check_daily_reset()
                
                # Global Kill Switch Evaluation
                kill_switch_active = False
                if self.daily_trades >= MAX_DAILY_TRADES or self.daily_pnl_inr <= MAX_DAILY_LOSS_INR:
                    kill_switch_active = True
                
                # Aggregate Risk Check
                active_trades_count = sum(1 for p in self.positions.values() if p is not None)

                for pair in self.pairs:
                    # Execute on Higher Timeframes
                    df_4h = fetch_live_data(pair, "4h", 100)
                    time.sleep(1)
                    df_1h = fetch_live_data(pair, "1h", 100)
                    time.sleep(1)
                    
                    if df_4h.empty or df_1h.empty: continue
                    
                    # 4H Macro Regime
                    df_4h['EMA_20'] = df_4h['close'].ewm(span=20, adjust=False).mean()
                    df_4h['EMA_50'] = df_4h['close'].ewm(span=50, adjust=False).mean()
                    df_4h['ema_spread'] = (df_4h['EMA_20'] - df_4h['EMA_50']).abs() / df_4h['close'] * 100
                    
                    closed_4h = df_4h.iloc[-2]
                    is_bull = (closed_4h['EMA_20'] > closed_4h['EMA_50']) and (closed_4h['ema_spread'] > 0.15)
                    is_bear = (closed_4h['EMA_20'] < closed_4h['EMA_50']) and (closed_4h['ema_spread'] > 0.15)

                    # 1H Execution Trigger & Volatility
                    df_1h['SMA_20'] = df_1h['close'].rolling(window=20).mean()
                    df_1h['STD_20'] = df_1h['close'].rolling(window=20).std()
                    df_1h['BBL'] = df_1h['SMA_20'] - (df_1h['STD_20'] * 2.0)
                    df_1h['BBU'] = df_1h['SMA_20'] + (df_1h['STD_20'] * 2.0)
                    
                    tr = pd.concat([df_1h['high'] - df_1h['low'], 
                                    (df_1h['high'] - df_1h['close'].shift()).abs(), 
                                    (df_1h['low'] - df_1h['close'].shift()).abs()], axis=1).max(axis=1)
                    df_1h['ATR'] = tr.rolling(window=14).mean()
                    
                    closed_1h = df_1h.iloc[-2]
                    live_price = df_1h.iloc[-1]['close']
                    
                    # 1. Manage Active Positions Deterministically
                    if self.positions[pair] is not None:
                        pos = self.positions[pair]
                        side, sl, tp = pos['side'], pos['sl'], pos['tp']
                        
                        # Stagnation Timeout (14 Hours)
                        hours_held = (datetime.now() - pos['entry_time']).total_seconds() / 3600
                        if hours_held >= 14:
                            self.close_trade(pair, live_price, "Time Stagnation Timeout")
                            continue

                        # Breakeven Ratchet (+1R Trailing)
                        if not pos['be_moved']:
                            if (side == 'LONG' and live_price >= pos['be_trigger']) or \
                               (side == 'SHORT' and live_price <= pos['be_trigger']):
                                self.positions[pair]['sl'] = pos['be_sl']
                                self.positions[pair]['be_moved'] = True
                                send_telegram_alert(f"🛡️ <b>BREAKEVEN RATCHET</b>\n{pair} hit +1R. Stop loss trailed to entry. Risk neutralized.")
                        
                        # Stop Loss / Take Profit Resolution
                        if side == 'LONG':
                            if live_price <= sl: self.close_trade(pair, live_price, "Stop Loss")
                            elif live_price >= tp: self.close_trade(pair, live_price, "Take Profit")
                        else:
                            if live_price >= sl: self.close_trade(pair, live_price, "Stop Loss")
                            elif live_price <= tp: self.close_trade(pair, live_price, "Take Profit")
                        continue

                    # 2. Prevent New Entries if Guardrails are Triggered
                    if kill_switch_active or active_trades_count >= MAX_CONCURRENT_POSITIONS:
                        continue

                    # 3. Dynamic Position Sizing (Risk Locked to ₹250)
                    risk_usd = RISK_PER_TRADE_INR / USDT_INR_RATE
                    atr = closed_1h['ATR']
                    
                    if is_bull and closed_1h['close'] <= closed_1h['BBL']:
                        sl, tp = live_price - (atr * 2.0), live_price + (atr * 4.0)
                        size = risk_usd / (live_price - sl)
                        exec_price = live_price * (1 + SLIPPAGE_RATE)
                        self.open_trade(pair, 'LONG', exec_price, sl, tp, size, atr)
                        active_trades_count += 1
                        
                    elif is_bear and closed_1h['close'] >= closed_1h['BBU']:
                        sl, tp = live_price + (atr * 2.0), live_price - (atr * 4.0)
                        size = risk_usd / (sl - live_price)
                        exec_price = live_price * (1 - SLIPPAGE_RATE)
                        self.open_trade(pair, 'SHORT', exec_price, sl, tp, size, atr)
                        active_trades_count += 1
                        
                time.sleep(60)
            except Exception as e:
                time.sleep(60)

    def open_trade(self, pair, side, ep, sl, tp, size, atr):
        # Calculate Breakeven triggers (covering round-trip fees)
        be_trigger = ep + (atr * 2.0) if side == 'LONG' else ep - (atr * 2.0)
        cost_buffer = ep * ((FEE_RATE * 2) + (SLIPPAGE_RATE * 2))
        be_sl = ep + cost_buffer if side == 'LONG' else ep - cost_buffer
        
        self.positions[pair] = {
            'side': side, 'entry': ep, 'sl': sl, 'tp': tp, 'size': size,
            'be_trigger': be_trigger, 'be_sl': be_sl, 'be_moved': False,
            'entry_time': datetime.now()
        }
        self.daily_trades += 1
        
        msg = f"🟢 <b>NEW SWING TRADE</b>\nPair: {pair}\nSide: {side}\nEntry: ${ep:,.2f}\nSL: ${sl:,.2f}\nTP: ${tp:,.2f}"
        send_telegram_alert(msg)

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
        
        icon = "✅" if net_inr > 0 else ("⏱️" if "Timeout" in reason else "❌")
        msg = f"{icon} <b>TRADE CLOSED</b>\nPair: {pair}\nReason: {reason}\nNet PnL: ₹{net_inr:,.2f}\nDaily PnL: ₹{self.daily_pnl_inr:,.2f}"
        send_telegram_alert(msg)
        
        if self.daily_pnl_inr <= MAX_DAILY_LOSS_INR:
            send_telegram_alert(f"🛑 <b>LOSS LIMIT HIT</b>\nDaily max drawdown reached. Bot sleeping until midnight.")

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
st.title("⚡ AMTE Swing Engine (₹1 Lakh Baseline)")

df_ledger = pd.read_csv(TRADE_LOG_FILE) if os.path.exists(TRADE_LOG_FILE) else pd.DataFrame()
total_net_inr = df_ledger['Net_PnL_INR'].sum() if not df_ledger.empty and 'Net_PnL_INR' in df_ledger.columns else 0.0
current_bal_inr = CAPITAL_INR + total_net_inr

# Guardrail Status Checks
kill_switch_status = "🟢 ACTIVE"
if bot_instance.daily_trades >= MAX_DAILY_TRADES or bot_instance.daily_pnl_inr <= MAX_DAILY_LOSS_INR:
    kill_switch_status = "🔴 TRIGGERED (Sleeping)"
active_trades_count = sum(1 for p in bot_instance.positions.values() if p is not None)
risk_status = "🟡 CAPPED (Max 2)" if active_trades_count >= MAX_CONCURRENT_POSITIONS else "🟢 ACTIVE"

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Account Balance (INR)", f"₹{current_bal_inr:,.2f}", f"₹{total_net_inr:,.2f}")
col2.metric("Active Positions", f"{active_trades_count} / {MAX_CONCURRENT_POSITIONS}")
col3.metric("Today's Trades", f"{bot_instance.daily_trades} / {MAX_DAILY_TRADES}")
col4.metric("Today's Net PnL", f"₹{bot_instance.daily_pnl_inr:,.2f}")
col5.metric("System Overrides", kill_switch_status)

st.markdown("---")

# ==========================================
# INTERACTIVE STRATEGY CHARTS & OVERLAYS
# ==========================================
st.subheader("📊 1H Live Strategy Charts & Bollinger Bands")
selected_pair = st.selectbox("Select Asset Pair:", PAIRS)

df_chart = fetch_live_data(selected_pair, "1h", 100)
if not df_chart.empty:
    df_chart['SMA20'] = df_chart['close'].rolling(20).mean()
    df_chart['STD'] = df_chart['close'].rolling(20).std()
    df_chart['BBL'] = df_chart['SMA20'] - (df_chart['STD'] * 2.0)
    df_chart['BBU'] = df_chart['SMA20'] + (df_chart['STD'] * 2.0)

    fig = go.Figure()
    
    fig.add_trace(go.Candlestick(
        x=df_chart.index,
        open=df_chart['open'], high=df_chart['high'],
        low=df_chart['low'], close=df_chart['close'],
        name="1H Candles"
    ))
    
    fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart['BBU'], line=dict(color='rgba(150, 150, 150, 0.5)', width=1), name="Upper BB"))
    fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart['BBL'], line=dict(color='rgba(150, 150, 150, 0.5)', width=1), fill='tonexty', fillcolor='rgba(100, 100, 255, 0.05)', name="Lower BB"))
    fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart['SMA20'], line=dict(color='#2962FF', width=1.5), name="SMA 20"))

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
    st.warning("Fetching 1H market data from CoinDCX...")

st.markdown("---")

st.subheader("📜 AMTE Trade Ledger (Swing Executions)")
if not df_ledger.empty:
    st.dataframe(df_ledger.sort_index(ascending=False), use_container_width=True)
else:
    st.info("No trades executed yet. The swing pipeline is evaluating 4H regimes.")
