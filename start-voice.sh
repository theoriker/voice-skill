#!/bin/bash
# Voice server startup script
# Launches cloudflared + uvicorn, captures tunnel URL, updates Twilio webhook
set -e

VOICE_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT=8765
LOG_DIR="/tmp"
umask 077  # Restrict new file permissions
CF_LOG="$LOG_DIR/cloudflared-voice.log"
UV_LOG="$LOG_DIR/twilio-voice.log"
PID_FILE="$LOG_DIR/voice-server.pids"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

cleanup() {
    echo -e "\n${RED}Shutting down...${NC}"
    if [ -f "$PID_FILE" ]; then
        while read pid; do
            kill "$pid" 2>/dev/null && echo "Killed PID $pid"
        done < "$PID_FILE"
        rm -f "$PID_FILE"
    fi
    # Kill by port as fallback
    for pid in $(ps aux | grep "uvicorn.*$PORT" | grep -v grep | awk '{print $2}'); do
        kill "$pid" 2>/dev/null
    done
    for pid in $(ps aux | grep "cloudflared.*$PORT" | grep -v grep | awk '{print $2}'); do
        kill "$pid" 2>/dev/null
    done
    exit 0
}
trap cleanup SIGINT SIGTERM

# Kill any existing processes on our port
echo "Cleaning up old processes..."
for pid in $(ps aux | grep "uvicorn.*$PORT\|cloudflared.*localhost:$PORT\|twilio_voice.*$PORT" | grep -v grep | awk '{print $2}'); do
    kill -9 "$pid" 2>/dev/null && echo "  Killed stale PID $pid"
done
sleep 2

# Verify port is free
python3 -c "import socket; s=socket.socket(); s.bind(('0.0.0.0',$PORT)); s.close()" 2>/dev/null || {
    echo -e "${RED}Port $PORT still in use. Waiting...${NC}"
    sleep 3
    python3 -c "import socket; s=socket.socket(); s.bind(('0.0.0.0',$PORT)); s.close()" || {
        echo -e "${RED}Cannot bind port $PORT. Exiting.${NC}"
        exit 1
    }
}

# Start uvicorn
echo "Starting voice server on port $PORT..."
cd "$VOICE_DIR"
nohup python3 -m uvicorn scripts.adapters.twilio_voice:app --host 127.0.0.1 --port $PORT > "$UV_LOG" 2>&1 &
UV_PID=$!
echo "$UV_PID" > "$PID_FILE"
sleep 2

# Verify server started
curl --max-time 3 -s -X POST "http://127.0.0.1:$PORT/voice" | grep -q "Connected to Theo" || {
    echo -e "${RED}Server failed to start. Check $UV_LOG${NC}"
    exit 1
}
echo -e "${GREEN}Voice server running (PID $UV_PID)${NC}"

# Start cloudflared tunnel
echo "Starting cloudflared tunnel..."
cloudflared tunnel --url "http://localhost:$PORT" > "$CF_LOG" 2>&1 &
CF_PID=$!
echo "$CF_PID" >> "$PID_FILE"

# Wait for tunnel URL (up to 15 seconds)
TUNNEL_URL=""
for i in $(seq 1 15); do
    TUNNEL_URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$CF_LOG" 2>/dev/null | head -1)
    if [ -n "$TUNNEL_URL" ]; then
        break
    fi
    sleep 1
done

if [ -z "$TUNNEL_URL" ]; then
    echo -e "${RED}Failed to get tunnel URL. Check $CF_LOG${NC}"
    cleanup
    exit 1
fi

TUNNEL_HOST=$(echo "$TUNNEL_URL" | sed 's|https://||')
echo -e "${GREEN}Tunnel: $TUNNEL_URL${NC}"

# Export PUBLIC_HOST for the server to pick up
export PUBLIC_HOST="$TUNNEL_HOST"

# Restart uvicorn with env var (binds to localhost only)
kill "$UV_PID" 2>/dev/null
sleep 2
PUBLIC_HOST="$TUNNEL_HOST" nohup python3 -m uvicorn scripts.adapters.twilio_voice:app --host 127.0.0.1 --port $PORT > "$UV_LOG" 2>&1 &
UV_PID=$!
# Update PID file
echo "$UV_PID" > "$PID_FILE"
echo "$CF_PID" >> "$PID_FILE"
sleep 2

# Verify through tunnel
echo "Testing tunnel..."
RESPONSE=$(curl --max-time 10 -s -X POST "$TUNNEL_URL/voice" 2>&1)
if echo "$RESPONSE" | grep -q "$TUNNEL_HOST"; then
    echo -e "${GREEN}Tunnel verified — TwiML URLs correct${NC}"
else
    echo -e "${RED}Tunnel test failed${NC}"
    echo "$RESPONSE"
fi

# Update Twilio webhook
echo "Updating Twilio webhook..."
python3 -c "
from twilio.rest import Client
import os
client = Client(os.environ['TWILIO_ACCOUNT_SID'], os.environ['TWILIO_AUTH_TOKEN'])
pn = client.incoming_phone_numbers('PN978940eb8ebf8cbacdb074793666bf57').update(
    voice_url='$TUNNEL_URL/voice'
)
print(f'  Webhook: {pn.voice_url}')
" 2>/dev/null

echo ""
echo -e "${GREEN}═══════════════════════════════════════${NC}"
echo -e "${GREEN}  Voice system ready!${NC}"
echo -e "${GREEN}  Phone: +1 (571) 444-8518${NC}"
echo -e "${GREEN}  Tunnel: $TUNNEL_URL${NC}"
echo -e "${GREEN}  Server PID: $UV_PID${NC}"
echo -e "${GREEN}  Tunnel PID: $CF_PID${NC}"
echo -e "${GREEN}═══════════════════════════════════════${NC}"
echo ""
echo "Monitoring processes... (Ctrl+C to stop)"

# Monitor loop — restart if either dies
while true; do
    if ! kill -0 "$UV_PID" 2>/dev/null; then
        echo -e "${RED}Server died — restarting...${NC}"
        cd "$VOICE_DIR"
        PUBLIC_HOST="$TUNNEL_HOST" nohup python3 -m uvicorn scripts.adapters.twilio_voice:app --host 127.0.0.1 --port $PORT > "$UV_LOG" 2>&1 &
        UV_PID=$!
        echo "$UV_PID" > "$PID_FILE"
        echo "$CF_PID" >> "$PID_FILE"
        echo -e "${GREEN}Server restarted (PID $UV_PID)${NC}"
    fi
    if ! kill -0 "$CF_PID" 2>/dev/null; then
        echo -e "${RED}Tunnel died — full restart needed (URL will change)${NC}"
        cleanup
        exec "$0"  # Re-run the entire script
    fi
    sleep 10
done
