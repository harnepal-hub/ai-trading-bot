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
USDT_INR_RATE = 86.00 # Standard exchange rate for calculation

FEE_RATE = 0.001       # 0.1% exchange fee per side
SLIPPAGE_RATE = 0.0005 # 0.05% execution slippage

TRADE_LOG_FILE = "amte_ledger.csv"

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
            print(f"🔄 Daily AMTE limits reset for {today}")

    def run_loop(self):
        time.sleep(5)
        while True:
            try:
                self.check_daily_reset()
                
                # Global Kill Switches
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
                    
                    # REGIME EVALUATION (Strictly Closed 1H Candle)
                    df_1h['EMA_20'] = df_1h['close'].ewm(span=20, adjust=False).mean()
                    df_1h['EMA_50'] = df_1h['close'].ewm(span=50, adjust=False).mean()
                    df_1h['ema_spread'] = (df_1h['EMA_20'] - df_1h['EMA_50']).abs() / df_1h['close'] * 100
                    
                    closed_1h = df_1h.iloc[-2]
                    is_bull = (closed_1h['EMA_20'] > closed_1h['EMA_50']) and (closed_1h['ema_spread'] > 0.15)
                    is_bear = (closed_1h['EMA_20'] < closed_1h['EMA_50']) and (closed_1h['ema_spread'] > 0.15)

                    # SIGNAL EVALUATION (Strictly Closed 15m Candle)
                    df_15m['SMA_20'] = df_15m['close'].rolling(window=20).mean()
                    df_15m['STD_20'] = df_15m['close'].rolling(window=20).std()
                    df_15m['BBL'] = df_15m['SMA_20'] - (df_15m['STD_20'] * 2.0)
                    df_15m['BBU'] = df_15m['SMA_20'] + (df_15m['STD_20'] * 2.0)
                    
                    tr = pd.concat([df_15m['high'] - df_15m['low'], 
                                    (df_15m['high'] - df_15m['close'].shift()).abs(), 
                                    (df_15m['low'] - df_15m['close'].shift()).abs()], axis=1).max(axis=1)
                    df_15m['ATR'] = tr.rolling(window=14).mean()
                    
                    closed_15m = df_15m.iloc[-2]
                    
                    # DETERMINISTIC EXECUTION (Live Snapshot Price)
                    live_price = df_15m.iloc[-1]['close']
                    
                    # Manage Active Positions
                    if self.positions[pair] is not None:
                        pos = self.positions[pair]
                        side, ep, sl, tp, size = pos['side'], pos['entry'], pos['sl'], pos['tp'], pos['size']
                        
                        # Evaluate against CURRENT live price to prevent wick-ambiguity look-ahead bias
                        if side == 'LONG':
                            if live_price <= sl: self.close_trade(pair, live_price, "Stop Loss")
                            elif live_price >= tp: self.close_trade(pair, live_price, "Take Profit")
                        else:
                            if live_price >= sl: self.close_trade(pair, live_price, "Stop Loss")
                            elif live_price <= tp: self.close_trade(pair, live_price, "Take Profit")
                        continue

                    # Execute New Entries if Kill Switch is OFF
                    if kill_switch_active:
                        continue

                    risk_usd = RISK_PER_TRADE_INR / USDT_INR_RATE
                    atr = closed_15m['ATR']
                    
                    if is_bull and closed_15m['close'] <= closed_15m['BBL']:
                        sl, tp = live_price - (atr * 2.0), live_price + (atr * 4.0)
                        size = risk_usd / (live_price - sl)
                        # Add Slippage Penalty to Entry
                        exec_price = live_price * (1 + SLIPPAGE_RATE)
                        self.open_trade(pair, 'LONG', exec_price, sl, tp, size)
                        
                    elif is_bear and closed_15m['close'] >= closed_15m['BBU']:
                        sl, tp = live_price + (atr * 2.0), live_price - (atr * 4.0)
                        size = risk_usd / (sl - live_price)
                        # Add Slippage Penalty to Entry
                        exec_price = live_price * (1 - SLIPPAGE_RATE)
                        self.open_trade(pair, 'SHORT', exec_price, sl, tp, size)
                        
                time.sleep(60)
            except Exception as e:
                time.sleep(60)

    def open_trade(self, pair, side, ep, sl, tp, size):
        self.positions[pair] = {'side': side, 'entry': ep, 'sl': sl, 'tp': tp, 'size': size}
        self.daily_trades += 1

    def close_trade(self, pair, exit_price, reason):
        pos = self.positions[pair]
        
        # Add Slippage Penalty to Exit
        actual_exit = exit_price * (1 - SLIPPAGE_RATE) if pos['side'] == 'LONG' else exit_price * (1 + SLIPPAGE_RATE)
        
        # True AMTE Cost Accounting
        gross_usd = (actual_exit - pos['entry']) * pos['size'] if pos['side'] == 'LONG' else (pos['entry'] - actual_exit) * pos['size']
        entry_notional = pos['entry'] * pos['size']
        exit_notional = actual_exit * pos['size']
        fees_usd = (entry_notional * FEE_RATE) + (exit_notional * FEE_RATE)
        
        net_usd = gross_usd - fees_usd
        net_inr = net_usd * USDT_INR_RATE
        
        self.daily_pnl_inr += net_inr
        
        log_trade_to_csv({
            'Time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'Pair': pair,
            'Side': pos['side'],
            'Entry': round(pos['entry'], 2),
            'Exit': round(actual_exit, 2),
            'Reason': reason,
            'Gross_USD': round(gross_usd, 2),
            'Fees_USD': round(fees_usd, 2),
            'Slippage_USD': round(abs(exit_price - actual_exit) * pos['size'], 2),
            'Net_PnL_INR': round(net_inr, 2)
        })
        self.positions[pair] = None

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

# Daily Guardrails Check
kill_switch_status = "🟢 ACTIVE"
if bot_instance.daily_trades >= MAX_DAILY_TRADES or bot_instance.daily_pnl_inr <= MAX_DAILY_LOSS_INR:
    kill_switch_status = "🔴 TRIGGERED (Sleeping until midnight)"

col1, col2, col3, col4 = st.columns(4)
col1.metric("Account Balance (INR)", f"₹{current_bal_inr:,.2f}", f"₹{total_net_inr:,.2f}")
col2.metric("Today's Trades", f"{bot_instance.daily_trades} / {MAX_DAILY_TRADES}")
col3.metric("Today's PnL", f"₹{bot_instance.daily_pnl_inr:,.2f}")
col4.metric("Daily Kill Switch", kill_switch_status)

st.markdown("---")
st.subheader("📜 AMTE Trade Ledger (Cost-Aware)")
if not df_ledger.empty:
    st.dataframe(df_ledger.sort_index(ascending=False), use_container_width=True)
else:
    st.info("No trades executed yet. The AMTE pipeline is strictly monitoring closed candles.")
