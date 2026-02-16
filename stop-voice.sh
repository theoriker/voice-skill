#!/bin/bash
# Stop the voice server and tunnel
PID_FILE="/tmp/voice-server.pids"

if [ -f "$PID_FILE" ]; then
    while read pid; do
        kill "$pid" 2>/dev/null && echo "Killed PID $pid"
    done < "$PID_FILE"
    rm -f "$PID_FILE"
fi

# Kill by pattern as fallback
for pid in $(ps aux | grep -E "uvicorn.*8765|cloudflared.*localhost:8765|twilio_voice.*8765" | grep -v grep | awk '{print $2}'); do
    kill -9 "$pid" 2>/dev/null && echo "Killed stale PID $pid"
done

echo "Voice server stopped."
