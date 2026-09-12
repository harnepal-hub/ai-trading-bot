import ccxt
import pandas as pd
import pandas_ta as ta
import time
from datetime import datetime

def initialize_exchange():
    """
    Initializes the connection to Binance. 
    We use Binance for data fetching because their API is the fastest and most reliable for crypto.
    (We can route the actual orders to CoinDCX later if needed).
    """
    print("Connecting to Exchange...")
    exchange = ccxt.binance({
        'enableRateLimit': True,
        'options': {
            'defaultType': 'spot' # We are trading spot. Change to 'future' for futures.
        }
    })
    return exchange

def fetch_market_data(exchange, symbol='BTC/USDT', timeframe='1m', limit=100):
    """
    Fetches the latest OHLCV (Open, High, Low, Close, Volume) data.
    We use the 1-minute timeframe to give the AI high-resolution data.
    """
    try:
        # Fetch data from the exchange
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        # Convert to a Pandas DataFrame for easy manipulation
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # Convert timestamp to a readable datetime format and set it as the index
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('datetime', inplace=True)
        
        # Ensure all columns are numeric floats
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
            
        return df
    
    except Exception as e:
        print(f"Error fetching data: {e}")
        return None

def calculate_indicators(df):
    """
    Applies our specific strategy indicators to the DataFrame.
    The AI will use these columns as its "State" to make decisions.
    """
    if df is None or len(df) == 0:
        return df

    # 1. VWAP (Volume Weighted Average Price) - The Institutional Anchor
    # Requires high, low, close, and volume
    df['VWAP'] = ta.vwap(high=df['high'], low=df['low'], close=df['close'], volume=df['volume'])
    
    # 2. Bollinger Bands (20, 2) - For volatility and overextension
    bbands = ta.bbands(df['close'], length=20, std=2)
    # pandas_ta returns multiple columns, we just need the lower and upper bands
    if bbands is not None:
        df['BB_LOWER'] = bbands['BBL_20_2.0']
        df['BB_UPPER'] = bbands['BBU_20_2.0']
        df['BB_MID'] = bbands['BBM_20_2.0']
    
    # 3. RSI (14) - For Momentum Exhaustion
    df['RSI'] = ta.rsi(df['close'], length=14)
    
    # 4. ATR (14) - For dynamic Stop Loss sizing
    df['ATR'] = ta.atr(high=df['high'], low=df['low'], close=df['close'], length=14)
    
    # 5. Volume Rate of Change (RoC) - To solve the "Lag" problem!
    # This looks at how fast volume is exploding right now compared to 3 minutes ago.
    df['VOL_ROC'] = df['volume'].pct_change(periods=3) * 100

    # Drop any rows that have NaN values (which happen at the start of indicator calculations)
    df.dropna(inplace=True)
    
    return df

if __name__ == "__main__":
    print("=== AI Trading Data Engine Started ===")
    
    # Initialize
    exchange = initialize_exchange()
    symbol_to_trade = 'BTC/USDT'
    
    print(f"Fetching initial dataset for {symbol_to_trade}...")
    
    # Fetch 100 candles of 1-minute data
    raw_data = fetch_market_data(exchange, symbol=symbol_to_trade, timeframe='1m', limit=100)
    
    if raw_data is not None:
        # Calculate indicators
        processed_data = calculate_indicators(raw_data)
        
        # Display the most recent row of data (what the AI will see right now)
        latest_state = processed_data.iloc[-1]
        
        print("\n--- CURRENT MARKET STATE (AI VISION) ---")
        print(f"Time:       {latest_state.name}")
        print(f"Price:      ${latest_state['close']:,.2f}")
        print(f"VWAP:       ${latest_state['VWAP']:,.2f}")
        print(f"BB Lower:   ${latest_state['BB_LOWER']:,.2f}")
        print(f"BB Upper:   ${latest_state['BB_UPPER']:,.2f}")
        print(f"RSI:        {latest_state['RSI']:.2f}")
        print(f"ATR:        ${latest_state['ATR']:,.2f} (Use for Stop Loss)")
        print(f"Vol RoC:    {latest_state['VOL_ROC']:.2f}% (Momentum Velocity)")
        print("----------------------------------------\n")
        
        print("Setup successful! The AI can now see the market.")
    else:
        print("Failed to initialize market data.")
