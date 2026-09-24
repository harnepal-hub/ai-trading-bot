import os
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
import requests

st.set_page_config(page_title="AMTE Quantitative Terminal", layout="wide")
st.title("⚡ AMTE Multi-Model Terminal (Read-Only)")

def fetch_chart_data(pair, interval, limit=100):
    url = f"https://public.coindcx.com/market_data/candles?pair={pair}&interval={interval}&limit={limit}"
    try:
        response = requests.get(url, timeout=5)
        df = pd.DataFrame(response.json())
        df['datetime'] = pd.to_datetime(df['time'], unit='ms')
        df.set_index(pd.DatetimeIndex(df["datetime"]), inplace=True)
        for col in ['open', 'high', 'low', 'close']: df[col] = df[col].astype(float)
        return df.sort_index()
    except: return pd.DataFrame()

tab1, tab2, tab3 = st.tabs(["⚡ AMTE", "🎯 TW Orig", "🛠️ TW Tuned"])

with tab1:
    ledger_file = "amte_ledger.csv"
    if os.path.exists(ledger_file):
        df_amte = pd.read_csv(ledger_file)
        st.metric("Total Paper Trades", len(df_amte))
        st.dataframe(df_amte.sort_index(ascending=False), use_container_width=True)
    
    pair = st.selectbox("View Market Context:", ["B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT"])
    df_chart = fetch_chart_data(pair, "15m", 100)
    
    if not df_chart.empty:
        fig = go.Figure(data=[go.Candlestick(x=df_chart.index, open=df_chart['open'], high=df_chart['high'], low=df_chart['low'], close=df_chart['close'])])
        fig.update_layout(height=450, template="plotly_dark", xaxis_rangeslider_visible=False)
        st.plotly_chart(fig, use_container_width=True)
        
# Repeat for Tab 2 and Tab 3...
