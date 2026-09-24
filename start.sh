#!/bin/bash

# Start the async backend engine in the background
python engine.py &

# Start the Streamlit frontend in the foreground
streamlit run dashboard.py --server.port $PORT --server.address 0.0.0.0
