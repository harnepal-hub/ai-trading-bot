import os
import time
import base64
import threading
import warnings
import math
from datetime import datetime
import pytz
import requests
import pandas as pd
import numpy as np
import streamlit as st
import plotly.graph_objects as go
import yfinance as yf

warnings.filterwarnings('ignore')

# ==========================================
# CONFIGURATION & SECRETS
# ==========================================
PAIRS = [BTC-USD, ETH-USD, SOL-USD, BNB-USD, XRP-USD]
CAPITAL_USD = 5000.00
RISK_PER_TRADE_USD = 25.00

IST = pytz.timezone('AsiaKolkata')

GITHUB_TOKEN = st.secrets.get(GITHUB_TOKEN, os.getenv(GITHUB_TOKEN, ))
REPO_NAME = st.secrets.get(REPO_NAME, os.getenv(REPO_NAME, ))

LEDGERS = {
    AMTE crypto_amte_ledger.csv,
    TW_ORIG crypto_tw_orig_ledger.csv,
    TW_TUNED crypto_tw_tuned_ledger.csv
}

TELEGRAM_BOT_TOKEN = st.secrets.get(TELEGRAM_BOT_TOKEN, os.getenv(TELEGRAM_BOT_TOKEN, ))
TELEGRAM_CHAT_ID = st.secrets.get(TELEGRAM_CHAT_ID, os.getenv(TELEGRAM_CHAT_ID, ))
LEDGER_COLUMNS = ['Time', 'Asset', 'Strategy', 'Side', 'Qty', 'Entry', 'Exit', 'Reason', 'Gross_USD', 'Exchange_Fee_USD', 'Net_PnL_USD', 'Max_DD_USD']

def send_telegram_alert(message)
    if not TELEGRAM_BOT_TOKEN return
    url = fhttpsapi.telegram.orgbot{TELEGRAM_BOT_TOKEN}sendMessage
    payload = {chat_id TELEGRAM_CHAT_ID, text message, parse_mode HTML}
    try requests.post(url, json=payload, timeout=5)
    except pass

# ==========================================
# GITHUB PERSISTENT SYNC (NO LOCAL SAVES)
# ==========================================
def load_ledger_from_github(strat_key)
    filename = LEDGERS[strat_key]
    if not GITHUB_TOKEN or not REPO_NAME return pd.DataFrame(columns=LEDGER_COLUMNS)
    url = fhttpsapi.github.comrepos{REPO_NAME}contents{filename}
    headers = {Authorization ftoken {GITHUB_TOKEN}, Accept applicationvnd.github.v3+json}
    res = requests.get(url, headers=headers)
    if res.status_code == 200
        content = base64.b64decode(res.json()[content]).decode(utf-8)
        from io import StringIO
        return pd.read_csv(StringIO(content))
    return pd.DataFrame(columns=LEDGER_COLUMNS)

def sync_trade_to_github(strat_key, trade_data)
    filename = LEDGERS[strat_key]
    df_existing = load_ledger_from_github(strat_key)
    df_new = pd.DataFrame([trade_data])
    df_combined = pd.concat([df_existing, df_new], ignore_index=True) if not df_existing.empty else df_new
    csv_str = df_combined.to_csv(index=False)
    
    if not GITHUB_TOKEN or not REPO_NAME return
    url = fhttpsapi.github.comrepos{REPO_NAME}contents{filename}
    headers = {Authorization ftoken {GITHUB_TOKEN}, Accept applicationvnd.github.v3+json}
    res = requests.get(url, headers=headers)
    sha = res.json().get(sha) if res.status_code == 200 else None
    
    payload = {message fAuto-log {strat_key} trade {trade_data.get('Asset')}, content base64.b64encode(csv_str.encode(utf-8)).decode(utf-8)}
    if sha payload[sha] = sha
    requests.put(url, headers=headers, json=payload, timeout=10)

# ==========================================
# MARKET DATA & COSTS
# ==========================================
def fetch_crypto_data(ticker, interval, period=5d)
    try
        df = yf.download(tickers=ticker, period=period, interval=interval, progress=False)
        if df.empty return pd.DataFrame()
        df.reset_index(inplace=True)
        if isinstance(df.columns, pd.MultiIndex) df.columns = df.columns.droplevel(1)
        df.rename(columns={'Datetime' 'datetime', 'Open' 'open', 'High' 'high', 'Low' 'low', 'Close' 'close', 'Volume' 'volume'}, inplace=True)
        df.set_index('datetime', inplace=True)
        for col in ['open', 'high', 'low', 'close', 'volume'] df[col] = df[col].astype(float)
        return df
    except return pd.DataFrame()

def calculate_crypto_costs(entry_price, exit_price, quantity)
    return ((entry_price  quantity)  0.0005) + ((exit_price  quantity)  0.0005)

def calculate_ehma(series, length=16)
    half_len = max(1, length  2)
    sqrt_len = max(1, int(round(math.sqrt(length))))
    return (2  series.ewm(span=half_len, adjust=False).mean() - series.ewm(span=length, adjust=False).mean()).ewm(span=sqrt_len, adjust=False).mean()

# ==========================================
# EVOLVED TRADING ENGINE (247)
# ==========================================
class UnifiedCryptoEngine
    def __init__(self, pairs)
        self.pairs = pairs
        self.strats = [AMTE, TW_ORIG, TW_TUNED]
        self.positions = {s {p {'status' 'NONE'} for p in pairs} for s in self.strats}
        self.current_date = datetime.now(IST).date()
        self.daily_metrics = {s {'trades' 0, 'pnl' 0.0} for s in self.strats}
        self.max_daily_trades = 10 
        self.max_daily_loss = -100.00 
        self.max_concurrent = 3

    def check_daily_reset(self, today_ist)
        if today_ist != self.current_date
            self.current_date = today_ist
            for s in self.strats
                self.daily_metrics[s] = {'trades' 0, 'pnl' 0.0}
                for p in self.pairs
                    if self.positions[s][p]['status'] == 'PENDING_ENTRY'
                        self.positions[s][p] = {'status' 'NONE'}

    def process_cycle(self)
        now_ist = datetime.now(IST)
        self.check_daily_reset(now_ist.date())
        
        for pair in self.pairs
            df_15m = fetch_crypto_data(pair, 15m, 5d)
            df_1h = fetch_crypto_data(pair, 1h, 1mo)
            time.sleep(1) 
            if df_15m.empty or df_1h.empty or len(df_15m)  105 continue

            df_1h['EMA20'] = df_1h['close'].ewm(span=20, adjust=False).mean()
            df_1h['EMA50'] = df_1h['close'].ewm(span=50, adjust=False).mean()
            macro_trend = UP if df_1h.iloc[-1]['EMA20']  df_1h.iloc[-1]['EMA50'] else DOWN

            df_15m['EMA100'] = df_15m['close'].ewm(span=100, adjust=False).mean()
            df_15m['MHULL'] = calculate_ehma(df_15m['close'], 16)
            df_15m['SHULL_2'] = df_15m['MHULL'].shift(2)
            df_15m['SHULL_3'] = df_15m['MHULL'].shift(3)
            
            df_15m['SMA20'] = df_15m['close'].rolling(20).mean()
            df_15m['STD20'] = df_15m['close'].rolling(20).std()
            df_15m['Upper_BB'] = df_15m['SMA20'] + (df_15m['STD20']  1.5)
            df_15m['Lower_BB'] = df_15m['SMA20'] - (df_15m['STD20']  1.5)
            
            tr = pd.concat([df_15m['high'] - df_15m['low'], (df_15m['high'] - df_15m['close'].shift()).abs(), (df_15m['low'] - df_15m['close'].shift()).abs()], axis=1).max(axis=1)
            df_15m['ATR'] = tr.rolling(14).mean()
            df_15m['ATR_50'] = df_15m['ATR'].rolling(50).mean()

            c_prev = df_15m.iloc[-3]
            c_curr = df_15m.iloc[-2]
            live_price = df_15m.iloc[-1]['close']
            atr = c_curr['ATR']
            signals = {AMTE None, TW_ORIG None, TW_TUNED None}

            if macro_trend == UP and c_curr['low'] = c_curr['Lower_BB'] signals[AMTE] = LONG
            elif macro_trend == DOWN and c_curr['high'] = c_curr['Upper_BB'] signals[AMTE] = SHORT
            if (c_prev['SHULL_2'] = c_prev['MHULL']) and (c_curr['SHULL_2']  c_curr['MHULL']) and (c_curr['close']  c_curr['EMA100']) signals[TW_ORIG] = LONG
            elif (c_prev['SHULL_2'] = c_prev['MHULL']) and (c_curr['SHULL_2']  c_curr['MHULL']) and (c_curr['close']  c_curr['EMA100']) signals[TW_ORIG] = SHORT
            if (c_prev['SHULL_3'] = c_prev['MHULL']) and (c_curr['SHULL_3']  c_curr['MHULL']) and (c_curr['close']  c_curr['EMA100']) and (c_curr['ATR']  c_curr['ATR_50']) signals[TW_TUNED] = LONG
            elif (c_prev['SHULL_3'] = c_prev['MHULL']) and (c_curr['SHULL_3']  c_curr['MHULL']) and (c_curr['close']  c_curr['EMA100']) and (c_curr['ATR']  c_curr['ATR_50']) signals[TW_TUNED] = SHORT

            for strat in self.strats
                # EVOLUTION Strict Baseline Filter for Shorts
                if signals[strat] == SHORT and live_price  df_1h.iloc[-1]['EMA50']
                    signals[strat] = None 

                pos = self.positions[strat][pair]
                if pos['status'] == 'ACTIVE'
                    ep = pos['limit_price']
                    gross = (live_price - ep)  pos['size'] if pos['side'] == 'LONG' else (ep - live_price)  pos['size']
                    self.positions[strat][pair]['max_dd_inr'] = min(pos.get('max_dd_inr', 0.0), gross - calculate_crypto_costs(ep, live_price, pos['size']))

                    # EVOLUTION ATR Trailing Stop Mechanics
                    if pos['side'] == 'LONG'
                        if live_price = ep + (atr  1.0) and pos['sl']  ep
                            pos['sl'] = ep + (atr  0.1) # Lock breakeven + fee coverage
                        
                        if live_price = pos['sl'] self.close_trade(strat, pair, pos['sl'], Trailing SL if pos['sl']  ep else Stop Market)
                        elif live_price = pos['tp'] self.close_trade(strat, pair, pos['tp'], Limit TP)
                    else
                        if live_price = ep - (atr  1.0) and pos['sl']  ep
                            pos['sl'] = ep - (atr  0.1)
                            
                        if live_price = pos['sl'] self.close_trade(strat, pair, pos['sl'], Trailing SL if pos['sl']  ep else Stop Market)
                        elif live_price = pos['tp'] self.close_trade(strat, pair, pos['tp'], Limit TP)
                    continue

                if pos['status'] == 'PENDING_ENTRY'
                    if df_15m.iloc[-1]['low'] = pos['limit_price'] if pos['side'] == 'LONG' else df_15m.iloc[-1]['high'] = pos['limit_price']
                        self.positions[strat][pair].update({'status' 'ACTIVE', 'entry_time' now_ist, 'max_dd_inr' 0.0})
                        self.daily_metrics[strat]['trades'] += 1
                        send_telegram_alert(f🟢 b[CRYPTO {strat}] FILLEDbnAsset {pair}nPrice ${pos['limit_price'],.4f})
                    continue

                kill_active = self.daily_metrics[strat]['trades'] = self.max_daily_trades or self.daily_metrics[strat]['pnl'] = self.max_daily_loss
                if not kill_active and sum(1 for p in self.positions[strat].values() if p['status'] == 'ACTIVE')  self.max_concurrent and signals[strat]
                    limit_p = c_curr['Lower_BB'] if signals[strat] == LONG and strat == AMTE else live_price
                    limit_p = c_curr['Upper_BB'] if signals[strat] == SHORT and strat == AMTE else limit_p
                    sl = limit_p - (atr  1.0) if signals[strat] == LONG else limit_p + (atr  1.0)
                    tp = limit_p + (atr  4.0) if signals[strat] == LONG else limit_p - (atr  4.0)
                    self.positions[strat][pair] = {'status' 'PENDING_ENTRY', 'side' signals[strat], 'limit_price' limit_p, 'sl' sl, 'tp' tp, 'size' RISK_PER_TRADE_USD  max(0.0001, abs(limit_p - sl)), 'max_dd_inr' 0.0}

    def close_trade(self, strat, pair, exec_price, reason)
        pos = self.positions[strat][pair]
        ep, qty = pos['limit_price'], pos['size']
        gross = (exec_price - ep)  qty if pos['side'] == 'LONG' else (ep - exec_price)  qty
        costs = calculate_crypto_costs(ep, exec_price, qty)
        net_usd = gross - costs
        self.daily_metrics[strat]['pnl'] += net_usd
        trade_record = {'Time' datetime.now(IST).strftime(%Y-%m-%d %H%M%S), 'Asset' pair, 'Strategy' strat, 'Side' pos['side'], 'Qty' round(qty, 4), 'Entry' round(ep, 4), 'Exit' round(exec_price, 4), 'Reason' reason, 'Gross_USD' round(gross, 2), 'Exchange_Fee_USD' round(costs, 2), 'Net_PnL_USD' round(net_usd, 2), 'Max_DD_USD' round(pos.get('max_dd_inr', 0.0), 2)}
        sync_trade_to_github(strat, trade_record)
        self.positions[strat][pair] = {'status' 'NONE'}

@st.cache_resource
def get_engine() return UnifiedCryptoEngine(PAIRS)
engine = get_engine()

@st.cache_resource
def start_background_loop(_engine)
    for t in threading.enumerate()
        if t.name == CryptoTraderThread return t 
    def loop()
        time.sleep(5)
        while True
            try
                _engine.process_cycle()
                time.sleep(300)
            except time.sleep(300)
    t = threading.Thread(target=loop, daemon=True, name=CryptoTraderThread)
    t.start()
    return t
start_background_loop(engine)

# ==========================================
# STREAMLIT UI & EVOLUTION ANALYTICS
# ==========================================
st.set_page_config(page_title=Crypto Multi-Model Terminal, layout=wide)
st.title(🪙 Crypto Multi-Model Terminal)
st.caption(fCurrent Time (IST) {datetime.now(IST).strftime('%Y-%m-%d %I%M %p')}  🟢 Market 247  🛡️ Evolved Baseline & Trailing Stops Active)
st.markdown(---)

tabs = st.tabs([Strategy A (AMTE), Strategy B (TW Orig), Strategy C (TW Tuned), 🧬 Evolution Analytics])

# RENDER STANDARD TABS
for i, strat in enumerate([AMTE, TW_ORIG, TW_TUNED])
    with tabs[i]
        df_led = load_ledger_from_github(strat)
        net_pnl = df_led['Net_PnL_USD'].sum() if not df_led.empty else 0.0
        c1, c2, c3, c4 = st.columns(4)
        c1.metric(Capital Balance, f${(CAPITAL_USD + net_pnl),.2f}, f${net_pnl,.2f})
        c2.metric(Market Exposure, f{sum(1 for p in engine.positions[strat].values() if p['status'] == 'ACTIVE')} Active  {sum(1 for p in engine.positions[strat].values() if p['status'] == 'PENDING_ENTRY')} Pending)
        c3.metric(Today's Trades, f{engine.daily_metrics[strat]['trades']}  {engine.max_daily_trades})
        c4.metric(Today's PnL, f${engine.daily_metrics[strat]['pnl'],.2f})
        
        if df_led.empty st.dataframe(pd.DataFrame(columns=LEDGER_COLUMNS), use_container_width=True)
        else st.dataframe(df_led.sort_index(ascending=False), use_container_width=True)

# RENDER EVOLUTION ANALYTICS TAB
with tabs[3]
    st.subheader(🧬 Strategy Evolution Sandbox)
    st.markdown(Simulate the mathematical impact of new rules against your historical `amt data.csv` dataset.)
    
    if os.path.exists('amt data.csv')
        df_amt = pd.read_csv('amt data.csv')
        df_amt['Time'] = pd.to_datetime(df_amt['Time'])
        df_amt = df_amt.sort_values('Time').reset_index(drop=True)
        
        st.markdown(### 🎛️ Control Panel)
        c1, c2, c3 = st.columns(3)
        pruned_assets = c1.multiselect(Prune Toxic Assets, df_amt['Pair'].unique(), default=['B-ETH_USDT'])
        ts_boost = c2.slider(Trailing Stop Boost Multiplier, 1.0, 2.0, 1.5, 0.1, help=Simulates capturing peak momentum before a timeout exit.)
        short_save = c3.slider(Short Filter Avoidance Rate, 0.0, 1.0, 0.5, 0.1, help=Simulates percentage of losing shorts prevented by the EMA50 baseline.)

        # Math Calculations
        df_proj = df_amt[~df_amt['Pair'].isin(pruned_assets)].copy()
        df_proj.loc[df_proj['Reason'] == 'Timeout (Profit)', 'Gross_USD'] = ts_boost
        df_proj['New_Net_USD'] = df_proj['Gross_USD'] - df_proj['Fees_USD'] - df_proj['Slippage_USD']
        df_proj['New_Net_INR'] = df_proj['New_Net_USD']  81.52 # Implied historical conversion rate
        
        short_loss_mask = (df_proj['Side'] == 'SHORT') & (df_proj['Reason'] == 'Stop Market') & (df_proj['New_Net_INR']  0)
        df_proj.loc[short_loss_mask, 'New_Net_INR'] = (1.0 - short_save)

        # Equity Curve Visualization
        st.markdown(### 📈 Cumulative Equity Curves)
        df_amt['Cum_Orig'] = df_amt['Net_PnL_INR'].cumsum()
        df_proj['Cum_Proj'] = df_proj['New_Net_INR'].cumsum()
        
        fig = go.Figure()
        fig.add_trace(go.Scatter(y=df_amt['Cum_Orig'], name='Original Framework', line=dict(color='#FF5252', dash='dot')))
        fig.add_trace(go.Scatter(y=df_proj['Cum_Proj'], name='Evolved Framework', line=dict(color='#00E676', width=3)))
        fig.update_layout(height=400, template=plotly_dark, margin=dict(l=10, r=10, t=30, b=10))
        st.plotly_chart(fig, use_container_width=True)

        # Performance Heatmap
        st.markdown(### 🔥 Asset Performance Heatmap (Projected))
        summary = df_proj.groupby('Pair').agg(Trades=('New_Net_INR', 'count'), Win_Rate=('New_Net_INR', lambda x (x  0).mean()  100), Net_PnL_INR=('New_Net_INR', 'sum')).reset_index()
        st.dataframe(summary.style.background_gradient(cmap='RdYlGn', subset=['Win_Rate', 'Net_PnL_INR']), use_container_width=True)
    else
        st.warning(⚠️ Upload `amt data.csv` to your GitHub repository root folder to unlock the interactive Evolution Sandbox.)
