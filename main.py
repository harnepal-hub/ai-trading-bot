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
# AMTE SYSTEM SPECIFICATION v3.1 (Action Mode)
# ==========================================
PAIRS = ["B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT", "B-XRP_USDT", "B-ADA_USDT"]
CAPITAL_INR = 100000.00
RISK_PER_TRADE_INR = 250.00
MAX_DAILY_TRADES = 5
MAX_DAILY_LOSS_INR = -1000.00
MAX_CONCURRENT_POSITIONS = 2
USDT_INR_RATE = 86.00 

# Institutional Fee Structure 
MAKER_FEE = 0.00025  # 0.025% for Resting Limit Orders (Entry & TP)
TAKER_FEE = 0.00050  # 0.050% for Market Orders (SL & Timeouts)
SLIPPAGE_RATE = 0.0005 

TRADE_LOG_FILE = "amte_ledger.csv"
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
# ADVANCED ORDER MANAGEMENT ENGINE
# ==========================================
class AMTEBot:
    def __init__(self, pairs):
        self.pairs = pairs
        self.positions = {pair: {'status': 'NONE'} for pair in pairs}
        self.current_date = date.today()
        self.daily_trades = 0
        self.daily_pnl_inr = 0.0
        
        if not os.path.exists(TRADE_LOG_FILE):
            pd.DataFrame(columns=[
                'Time', 'Pair', 'Side', 'Entry_Price', 'Exit_Price', 'Reason', 
                'Gross_USD', 'Fees_USD', 'Slippage_USD', 'Net_PnL_INR'
            ]).to_csv(TRADE_LOG_FILE, index=False)

    def check_daily_reset(self):
        today = date.today()
        if today != self.current_date:
            self.current_date = today
            self.daily_trades = 0
            self.daily_pnl_inr = 0.0
            for p in self.positions:
                if self.positions[p]['status'] == 'PENDING_ENTRY':
                    self.positions[p] = {'status': 'NONE'}
            send_telegram_alert("🔄 <b>AMTE Pipeline Reset</b>\nPending limit orders cleared for the new trading day.")

    def run_loop(self):
        time.sleep(5)
        send_telegram_alert("🚀 <b>AMTE Action Mode Live</b>\nHunting 15m dips with 1H trend alignment.")
        
        while True:
            try:
                self.check_daily_reset()
                
                kill_switch_active = self.daily_trades >= MAX_DAILY_TRADES or self.daily_pnl_inr <= MAX_DAILY_LOSS_INR
                active_trades_count = sum(1 for p in self.positions.values() if p['status'] == 'ACTIVE')

                for pair in self.pairs:
                    # Loosened Filters: 1H Macro, 15m Trigger
                    df_macro = fetch_live_data(pair, "1h", 100)
                    time.sleep(1)
                    df_micro = fetch_live_data(pair, "15m", 100)
                    time.sleep(1)
                    
                    if df_macro.empty or df_micro.empty: continue
                    
                    df_macro['EMA_20'] = df_macro['close'].ewm(span=20, adjust=False).mean()
                    df_macro['EMA_50'] = df_macro['close'].ewm(span=50, adjust=False).mean()
                    df_macro['ema_spread'] = (df_macro['EMA_20'] - df_macro['EMA_50']).abs() / df_macro['close'] * 100
                    
                    closed_macro = df_macro.iloc[-2]
                    is_bull = (closed_macro['EMA_20'] > closed_macro['EMA_50']) and (closed_macro['ema_spread'] > 0.05)
                    is_bear = (closed_macro['EMA_20'] < closed_macro['EMA_50']) and (closed_macro['ema_spread'] > 0.05)

                    df_micro['SMA_20'] = df_micro['close'].rolling(window=20).mean()
                    df_micro['STD_20'] = df_micro['close'].rolling(window=20).std()
                    # Loosened BB to 1.5 standard deviations
                    df_micro['BBL'] = df_micro['SMA_20'] - (df_micro['STD_20'] * 1.5)
                    df_micro['BBU'] = df_micro['SMA_20'] + (df_micro['STD_20'] * 1.5)
                    
                    tr = pd.concat([df_micro['high'] - df_micro['low'], 
                                    (df_micro['high'] - df_micro['close'].shift()).abs(), 
                                    (df_micro['low'] - df_micro['close'].shift()).abs()], axis=1).max(axis=1)
                    df_micro['ATR'] = tr.rolling(window=14).mean()
                    
                    closed_micro = df_micro.iloc[-2]
                    live_candle = df_micro.iloc[-1]
                    live_price = live_candle['close']
                    
                    pos = self.positions[pair]
                    
                    # 1. PROCESS ACTIVE TRADES
                    if pos['status'] == 'ACTIVE':
                        side, sl, tp = pos['side'], pos['sl'], pos['tp']
                        
                        # Timeout adapted for 15m triggers (4 hours)
                        hours_held = (datetime.now() - pos['entry_time']).total_seconds() / 3600
                        if hours_held >= 4:
                            self.close_trade(pair, live_price, "Time Timeout", order_type="TAKER")
                            continue

                        # Trailing Breakeven
                        if not pos['be_moved']:
                            if (side == 'LONG' and live_price >= pos['be_trigger']) or \
                               (side == 'SHORT' and live_price <= pos['be_trigger']):
                                self.positions[pair]['sl'] = pos['be_sl']
                                self.positions[pair]['be_moved'] = True
                        
                        if side == 'LONG':
                            if live_price <= sl: self.close_trade(pair, sl, "Stop Market Hit", order_type="TAKER")
                            elif live_price >= tp: self.close_trade(pair, tp, "Limit TP Filled", order_type="MAKER")
                        else:
                            if live_price >= sl: self.close_trade(pair, sl, "Stop Market Hit", order_type="TAKER")
                            elif live_price <= tp: self.close_trade(pair, tp, "Limit TP Filled", order_type="MAKER")
                        continue

                    # 2. PROCESS PENDING LIMIT ORDERS
                    if pos['status'] == 'PENDING_ENTRY':
                        if (pos['side'] == 'LONG' and not is_bull) or (pos['side'] == 'SHORT' and not is_bear):
                            self.positions[pair] = {'status': 'NONE'}
                            send_telegram_alert(f"🗑️ <b>LIMIT CANCELLED</b>\n{pair} regime shifted. Order pulled.")
                            continue
                            
                        if pos['side'] == 'LONG' and live_candle['low'] <= pos['limit_price']:
                            self.activate_trade(pair)
                        elif pos['side'] == 'SHORT' and live_candle['high'] >= pos['limit_price']:
                            self.activate_trade(pair)
                        continue

                    # 3. PLACE NEW LIMIT ORDERS
                    if kill_switch_active or active_trades_count >= MAX_CONCURRENT_POSITIONS:
                        continue

                    risk_usd = RISK_PER_TRADE_INR / USDT_INR_RATE
                    atr = closed_micro['ATR']
                    
                    if is_bull and live_price > closed_micro['BBL']:
                        limit_price = closed_micro['BBL']
                        sl, tp = limit_price - (atr * 2.0), limit_price + (atr * 4.0)
                        size = risk_usd / (limit_price - sl)
                        self.place_limit_order(pair, 'LONG', limit_price, sl, tp, size, atr)
                        
                    elif is_bear and live_price < closed_micro['BBU']:
                        limit_price = closed_micro['BBU']
                        sl, tp = limit_price + (atr * 2.0), limit_price - (atr * 4.0)
                        size = risk_usd / (sl - limit_price)
                        self.place_limit_order(pair, 'SHORT', limit_price, sl, tp, size, atr)
                        
                time.sleep(60)
            except Exception as e:
                time.sleep(60)

    def place_limit_order(self, pair, side, limit_price, sl, tp, size, atr):
        be_trigger = limit_price + (atr * 2.0) if side == 'LONG' else limit_price - (atr * 2.0)
        cost_buffer = limit_price * ((MAKER_FEE * 2) + TAKER_FEE)
        be_sl = limit_price + cost_buffer if side == 'LONG' else limit_price - cost_buffer
        
        self.positions[pair] = {
            'status': 'PENDING_ENTRY', 'side': side, 'limit_price': limit_price,
            'sl': sl, 'tp': tp, 'size': size, 'be_trigger': be_trigger, 
            'be_sl': be_sl, 'be_moved': False
        }
        msg = f"⏳ <b>RESTING LIMIT PLACED</b>\nPair: {pair}\nSide: {side}\nWait Price: ${limit_price:,.2f}"
        send_telegram_alert(msg)

    def activate_trade(self, pair):
        self.positions[pair]['status'] = 'ACTIVE'
        self.positions[pair]['entry_time'] = datetime.now()
        self.daily_trades += 1
        pos = self.positions[pair]
        msg = f"🟢 <b>ORDER FILLED (Maker)</b>\nPair: {pair}\nSide: {pos['side']}\nFilled: ${pos['limit_price']:,.2f}"
        send_telegram_alert(msg)

    def close_trade(self, pair, execution_price, reason, order_type):
        pos = self.positions[pair]
        entry_price = pos['limit_price']
        
        slippage = SLIPPAGE_RATE if order_type == "TAKER" else 0.0
        actual_exit = execution_price * (1 - slippage) if pos['side'] == 'LONG' else execution_price * (1 + slippage)
        
        gross_usd = (actual_exit - entry_price) * pos['size'] if pos['side'] == 'LONG' else (entry_price - actual_exit) * pos['size']
        
        entry_fee = (entry_price * pos['size']) * MAKER_FEE
        exit_fee = (actual_exit * pos['size']) * (MAKER_FEE if order_type == "MAKER" else TAKER_FEE)
        fees_usd = entry_fee + exit_fee
        
        net_usd = gross_usd - fees_usd
        net_inr = net_usd * USDT_INR_RATE
        self.daily_pnl_inr += net_inr
        
        log_trade_to_csv({
            'Time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'Pair': pair, 'Side': pos['side'],
            'Entry_Price': round(entry_price, 2), 'Exit_Price': round(actual_exit, 2),
            'Reason': reason, 'Gross_USD': round(gross_usd, 2),
            'Fees_USD': round(fees_usd, 2), 
            'Slippage_USD': round(abs(execution_price - actual_exit) * pos['size'], 2),
            'Net_PnL_INR': round(net_inr, 2)
        })
        self.positions[pair] = {'status': 'NONE'}
        
        icon = "✅" if net_inr > 0 else "❌"
        msg = f"{icon} <b>POSITION CLOSED</b>\nPair: {pair}\nReason: {reason}\nNet PnL: ₹{net_inr:,.2f}"
        send_telegram_alert(msg)

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
st.set_page_config(page_title="AMTE Institutional Engine", layout="wide")
st.title("⚡ AMTE Institutional Pipeline (Action Mode)")

df_ledger = pd.read_csv(TRADE_LOG_FILE) if os.path.exists(TRADE_LOG_FILE) else pd.DataFrame()
total_net_inr = df_ledger['Net_PnL_INR'].sum() if not df_ledger.empty and 'Net_PnL_INR' in df_ledger.columns else 0.0
current_bal_inr = CAPITAL_INR + total_net_inr

kill_switch_status = "🟢 ACTIVE"
if bot_instance.daily_trades >= MAX_DAILY_TRADES or bot_instance.daily_pnl_inr <= MAX_DAILY_LOSS_INR:
    kill_switch_status = "🔴 TRIGGERED"

active_count = sum(1 for p in bot_instance.positions.values() if p['status'] == 'ACTIVE')
pending_count = sum(1 for p in bot_instance.positions.values() if p['status'] == 'PENDING_ENTRY')

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Account Balance (INR)", f"₹{current_bal_inr:,.2f}", f"₹{total_net_inr:,.2f}")
col2.metric("Market Exposure", f"{active_count} Active / {pending_count} Pending")
col3.metric("Today's Trades", f"{bot_instance.daily_trades} / {MAX_DAILY_TRADES}")
col4.metric("Today's Net PnL", f"₹{bot_instance.daily_pnl_inr:,.2f}")
col5.metric("System Guardrails", kill_switch_status)

st.markdown("---")
st.subheader("📊 15m Active Order Routing & Tracking")
selected_pair = st.selectbox("Select Asset Pair:", PAIRS)

df_chart = fetch_live_data(selected_pair, "15m", 100)
if not df_chart.empty:
    df_chart['SMA20'] = df_chart['close'].rolling(20).mean()
    df_chart['STD'] = df_chart['close'].rolling(20).std()
    df_chart['BBL'] = df_chart['SMA20'] - (df_chart['STD'] * 1.5)
    df_chart['BBU'] = df_chart['SMA20'] + (df_chart['STD'] * 1.5)

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=df_chart.index, open=df_chart['open'], high=df_chart['high'],
        low=df_chart['low'], close=df_chart['close'], name="15m Candles"
    ))
    fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart['BBU'], line=dict(color='rgba(150, 150, 150, 0.5)', width=1), name="Upper BB"))
    fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart['BBL'], line=dict(color='rgba(150, 150, 150, 0.5)', width=1), fill='tonexty', fillcolor='rgba(100, 100, 255, 0.05)', name="Lower BB"))

    pos = bot_instance.positions[selected_pair]
    
    if pos['status'] == 'PENDING_ENTRY':
        fig.add_hline(y=pos['limit_price'], line_dash="dot", line_color="#BDBDBD", annotation_text=f"Resting Limit: ${pos['limit_price']:,.2f}")
    elif pos['status'] == 'ACTIVE':
        fig.add_hline(y=pos['limit_price'], line_dash="solid", line_color="#FF9800", annotation_text=f"Entry: ${pos['limit_price']:,.2f}")
        fig.add_hline(y=pos['tp'], line_dash="dash", line_color="#00E676", annotation_text=f"Take Profit: ${pos['tp']:,.2f}")
        fig.add_hline(y=pos['sl'], line_dash="dash", line_color="#FF5252", annotation_text=f"Stop Loss: ${pos['sl']:,.2f}")

    fig.update_layout(height=520, template="plotly_dark", xaxis_rangeslider_visible=False, margin=dict(l=15, r=15, t=20, b=15))
    st.plotly_chart(fig, use_container_width=True)
else:
    st.warning("Fetching 15m market data from CoinDCX...")

st.markdown("---")
st.subheader("📜 Institutional Ledger")
if not df_ledger.empty:
    st.dataframe(df_ledger.sort_index(ascending=False), use_container_width=True)
else:
    st.info("No limit orders filled yet. Waiting for market dips.")
