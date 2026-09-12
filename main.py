import requests
import pandas as pd
import numpy as np
import time
from datetime import datetime
import threading
from flask import Flask
import warnings
warnings.filterwarnings('ignore')

# --- CONFIGURATION ---
PAIRS = ["B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT"]
INITIAL_CAPITAL = 1200.00 

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

class EvolvingPaperBot:
    def __init__(self, pairs, capital):
        self.pairs = pairs
        self.balance = capital
        self.positions = {pair: None for pair in pairs}
        self.dna_bb_std = {pair: 2.0 for pair in pairs}
        
    def run_cycle(self):
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{current_time}] 👁️ AI SCANNING MARKET (Bal: ${self.balance:,.2f})")
        
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
                    if live_candle['low'] <= sl: self.close_trade(pair, sl, "Stop Loss", False, pos['be_moved'])
                    elif live_candle['high'] >= tp: self.close_trade(pair, tp, "Take Profit", True, pos['be_moved'])
                else:
                    if live_candle['high'] >= sl: self.close_trade(pair, sl, "Stop Loss", False, pos['be_moved'])
                    elif live_candle['low'] <= tp: self.close_trade(pair, tp, "Take Profit", True, pos['be_moved'])
                continue

            risk_amt = self.balance * 0.01
            if is_bull and close <= live_candle['BBL']:
                sl, tp = close - (atr * 2.0), close + (atr * 4.0)
                self.open_trade(pair, 'LONG', close, sl, tp, risk_amt / (close - sl), close + (atr * 2.0))
            elif is_bear and close >= live_candle['BBU']:
                sl, tp = close + (atr * 2.0), close - (atr * 4.0)
                self.open_trade(pair, 'SHORT', close, sl, tp, risk_amt / (sl - close), close - (atr * 2.0))

    def open_trade(self, pair, side, ep, sl, tp, size, be_trigger):
        self.positions[pair] = {'side': side, 'entry': ep, 'sl': sl, 'tp': tp, 'size': size, 'be_trigger': be_trigger, 'be_moved': False}
        print(f"🚀 [ENTRY] {pair} {side} @ ${ep:,.2f}")

    def close_trade(self, pair, exit_price, reason, is_win, is_be):
        pos = self.positions[pair]
        gross_pnl = (exit_price - pos['entry']) * pos['size'] if pos['side'] == 'LONG' else (pos['entry'] - exit_price) * pos['size']
        net_pnl = gross_pnl - ((exit_price * pos['size']) * 0.002) 
        self.balance += net_pnl
        print(f"✅ [EXIT] {pair} closed ({reason}) | Net PnL: ${net_pnl:,.2f}")
        
        if not is_be:
            if is_win: self.dna_bb_std[pair] = max(1.7, self.dna_bb_std[pair] - 0.1)
            else: self.dna_bb_std[pair] = min(2.8, self.dna_bb_std[pair] + 0.2)
        self.positions[pair] = None

# --- WEB SERVER WRAPPER ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is awake and trading!"

def run_bot_in_background():
    bot = EvolvingPaperBot(PAIRS, INITIAL_CAPITAL)
    while True:
        try:
            bot.run_cycle()
            time.sleep(60)
        except Exception as e:
            time.sleep(60)

if __name__ == "__main__":
    # 1. Start the trading bot in the background
    threading.Thread(target=run_bot_in_background, daemon=True).start()
    # 2. Start the fake website so Render gives us free hosting
    app.run(host='0.0.0.0', port=8080)
