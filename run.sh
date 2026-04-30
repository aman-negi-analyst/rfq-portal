#!/bin/bash
# Auto-restart script for Linux
while true; do
    echo "Starting RFQ Portal..."
    python3 app.py
    echo "App stopped. Restarting in 5 seconds..."
    sleep 5
done
