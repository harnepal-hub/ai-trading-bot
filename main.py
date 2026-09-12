import os
import sys
import time
import threading
import warnings
from datetime import datetime
import requests
import pandas as pd
import numpy as np
from flask import Flask

warnings.filterwarnings('ignore')

# ==========================================
# 1. BOT SETTINGS & CONFIGURATION
# ==========================================
PAIRS = ["B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT"]
INITIAL_CAPITAL = 1200.00  # Base capital (~1 Lakh INR)

def fetch_live_data(pair, interval, limit=100):
    """Fetches real-time candle data safely from CoinDCX."""
    url = f"https://public.coindcx.com/market_data/candles?pair={pair}&interval={interval}&limit={limit}"
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        if isinstance(data, dict):
            return pd.DataFrame()
        df = pd.DataFrame(data)
        df = df.sort_values(by='time').reset_index(drop=True)
        df['datetime'] = pd.to_datetime(df['time'], unit='ms')
        df.set_index(pd.DatetimeIndex(df["datetime"]), inplace=True)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
        return df
    except Exception:
        return pd.DataFrame()

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
