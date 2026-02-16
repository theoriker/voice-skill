#!/usr/bin/env python3
"""
Twilio Voice Adapter for OpenClaw — Async processing with thinking tone.

Flow:
  1. Call comes in → "Connected to Theo"
  2. <Gather speech> → Twilio transcribes user speech
  3. Return immediately with thinking tone (no timeout risk)
  4. Background task sends to gateway, waits for agent response
  5. When ready, Twilio REST API redirects call to deliver response
  6. <Say> response + loop back to step 2

Key improvement: webhook returns instantly, so Cloudflare tunnel
timeout is never hit regardless of how long the agent takes.
"""
import os
import sys
import json
import uuid
import base64
import asyncio
import logging
import time
import random
import re
from typing import Optional, Dict

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client as TwilioClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import websockets
from cryptography.hazmat.primitives.serialization import load_pem_private_key

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s')
logger = logging.getLogger('twilio-voice')

app = FastAPI(title="OpenClaw Voice - Twilio")

# ── Config ─────────────────────────────────────────────────

TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '')
TWILIO_PHONE = os.environ.get('TWILIO_PHONE', '+15714448518')
GATEWAY_URL = os.environ.get('OPENCLAW_GATEWAY_URL', 'ws://localhost:18789')
AGENT_SESSION = os.environ.get('OPENCLAW_SESSION', 'agent:main:voice')
PUBLIC_HOST = os.environ.get('PUBLIC_HOST', 'striking-regular-attendance-gather.trycloudflare.com')

ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'assets')

THINKING_PHRASES = [
    "One moment.",
    "Let me think about that.",
    "Hmm, give me a second.",
    "Working on it.",
    "Let me check.",
    "One sec.",
]

VOICE_CONTEXT = """[VOICE CALL] You are on a live phone call. Rules:
- Keep responses to 1-3 short sentences max
- No markdown, no formatting, no bullet points
- No tool calls (web search, exec, etc.) unless the caller explicitly asks you to look something up
- Be conversational and natural, like talking to a friend
- If you don't know something, just say so briefly
- Never say NO_REPLY or HEARTBEAT_OK on a voice call"""

# ── Pending Responses Store ────────────────────────────────

pending_responses: Dict[str, str] = {}


# ── Text Sanitization ─────────────────────────────────────

def sanitize_for_speech(text: str) -> str:
    """Clean text for TwiML <Say> — remove markdown, fix punctuation."""
    # Remove markdown formatting
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)  # bold
    text = re.sub(r'\*(.+?)\*', r'\1', text)        # italic
    text = re.sub(r'`(.+?)`', r'\1', text)          # inline code
    text = re.sub(r'#{1,6}\s+', '', text)            # headers
    text = re.sub(r'\[(.+?)\]\(.+?\)', r'\1', text) # links
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)  # bullet points
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)  # numbered lists

    # Fix special characters
    text = text.replace('—', ', ')   # em dash
    text = text.replace('–', ', ')   # en dash
    text = text.replace('"', '"').replace('"', '"')  # smart quotes
    text = text.replace(''', "'").replace(''', "'")
    text = text.replace('&', ' and ')
    text = text.replace('<', '').replace('>', '')  # XML safety

    # Collapse whitespace
    text = re.sub(r'\n{2,}', '. ', text)
    text = re.sub(r'\n', ' ', text)
    text = re.sub(r'\s{2,}', ' ', text)

    return text.strip()


# ── Gateway Bridge ─────────────────────────────────────────

class GatewayBridge:
    """Persistent async bridge to OpenClaw gateway."""

    _instance = None
    _lock = asyncio.Lock()

    def __init__(self):
        self.ws = None
        self.connected = False
        self._load_device_auth()

    def _load_device_auth(self):
        path = os.path.expanduser("~/.openclaw/identity/device.json")
        with open(path) as f:
            identity = json.load(f)
        self.device_id = identity["deviceId"]
        self.private_key = load_pem_private_key(identity["privateKeyPem"].encode(), password=None)
        pub_der = base64.b64decode("".join(identity["publicKeyPem"].strip().split("\n")[1:-1]))
        self.pub_b64url = base64.urlsafe_b64encode(pub_der[-32:]).rstrip(b"=").decode()
        paired_path = os.path.expanduser("~/.openclaw/devices/paired.json")
        with open(paired_path) as f:
            paired = json.load(f)
        entry = paired.get(self.device_id, {})
        tok = entry.get("tokens", {}).get("operator", {})
        self.device_token = tok.get("token", "")
        self.scopes = tok.get("scopes", ["operator.admin"])

    def _sign(self, payload):
        sig = self.private_key.sign(payload.encode())
        return base64.urlsafe_b64encode(sig).rstrip(b"=").decode()

    async def ensure_connected(self):
        if self.ws and self.connected:
            try:
                await self.ws.ping()
                return
            except Exception:
                self.connected = False

        self.ws = await websockets.connect(GATEWAY_URL)
        scopes_str = ",".join(self.scopes)

        ts = int(time.time() * 1000)
        parts = ["v1", self.device_id, "gateway-client", "backend", "operator",
                  scopes_str, str(ts), self.device_token]
        sig = self._sign("|".join(parts))

        await self.ws.send(json.dumps({
            "type": "req", "method": "connect", "id": "c-init",
            "params": {
                "minProtocol": 3, "maxProtocol": 3,
                "client": {"id": "gateway-client", "mode": "backend", "version": "1.0.0",
                           "platform": "twilio-voice", "displayName": "Twilio Voice",
                           "instanceId": f"tw-{uuid.uuid4().hex[:6]}"},
                "auth": {"token": self.device_token},
                "role": "operator", "scopes": self.scopes,
                "caps": ["chat.events"],
                "device": {"id": self.device_id, "publicKey": self.pub_b64url,
                           "signature": sig, "signedAt": ts}
            }
        }))

        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=10)
            frame = json.loads(raw)
            if frame.get("type") == "event" and frame.get("event") == "connect.challenge":
                nonce = frame["payload"]["nonce"]
                ts2 = int(time.time() * 1000)
                parts2 = ["v2", self.device_id, "gateway-client", "backend", "operator",
                           scopes_str, str(ts2), self.device_token, nonce]
                sig2 = self._sign("|".join(parts2))
                await self.ws.send(json.dumps({
                    "type": "req", "method": "connect", "id": "c-nonce",
                    "params": {
                        "minProtocol": 3, "maxProtocol": 3,
                        "client": {"id": "gateway-client", "mode": "backend", "version": "1.0.0",
                                   "platform": "twilio-voice", "displayName": "Twilio Voice",
                                   "instanceId": f"tw-{uuid.uuid4().hex[:6]}"},
                        "auth": {"token": self.device_token},
                        "role": "operator", "scopes": self.scopes,
                        "caps": ["chat.events"],
                        "device": {"id": self.device_id, "publicKey": self.pub_b64url,
                                   "signature": sig2, "signedAt": ts2, "nonce": nonce}
                    }
                }))
            elif frame.get("type") == "res" and frame.get("ok"):
                self.connected = True
                logger.info("Gateway connected ✓")
                break
            elif frame.get("type") == "res" and frame.get("error"):
                raise Exception(f"Gateway error: {frame['error']}")

    async def drain(self):
        """Drain stale events from WebSocket buffer."""
        while True:
            try:
                await asyncio.wait_for(self.ws.recv(), timeout=0.1)
            except (asyncio.TimeoutError, Exception):
                break

    async def send_and_wait(self, text, timeout=20):
        """Send message and wait for complete agent response. No timeout pressure."""
        await self.ensure_connected()
        await self.drain()

        req_id = f"tw-{uuid.uuid4().hex[:8]}"
        prefixed = f"{VOICE_CONTEXT}\n\n{text}"
        await self.ws.send(json.dumps({
            "type": "req", "method": "chat.send", "id": req_id,
            "params": {"sessionKey": AGENT_SESSION, "message": prefixed,
                       "idempotencyKey": str(uuid.uuid4())}
        }))
        logger.info(f"🎤 User: {text}")

        target_run_id = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                continue

            frame = json.loads(raw)
            ft = frame.get("type")

            if ft == "res" and frame.get("id") == req_id:
                if not frame.get("ok"):
                    logger.error(f"chat.send failed: {frame.get('error')}")
                    return "Sorry, something went wrong."
                continue

            if ft != "event" or frame.get("event") != "chat":
                continue

            payload = frame.get("payload", {})
            run_id = payload.get("runId", "")
            state = payload.get("state", "")

            if target_run_id is None:
                target_run_id = run_id
            elif run_id != target_run_id:
                continue

            if state == "final":
                msg = payload.get("message", {})
                content = msg.get("content", []) if isinstance(msg, dict) else []
                text_parts = []
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                result = "".join(text_parts).strip()
                if result and result not in ("NO", "NO_REPLY", "HEARTBEAT_OK") and not any(result.startswith(s) for s in ("NO_REPLY", "HEARTBEAT_OK")):
                    logger.info(f"🤖 Agent: {result[:150]}")
                    return result
                logger.info("🤖 (silent reply)")
                return None
            elif state == "error":
                logger.error(f"Agent error: {payload.get('errorMessage')}")
                return "Sorry, I hit an error. Try again."

        return "Sorry, that took too long. Could you ask again, maybe more simply?"

    @classmethod
    async def get_instance(cls):
        async with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance


# ── Background Processing ─────────────────────────────────

async def process_and_redirect(call_sid: str, speech_text: str):
    """Background task: get agent response, then redirect the call."""
    try:
        gateway = await GatewayBridge.get_instance()
        agent_response = await gateway.send_and_wait(speech_text)
    except Exception as e:
        logger.error(f"Gateway error in background: {e}")
        agent_response = "Sorry, I had trouble processing that. Try again."

    if not agent_response:
        # NO_REPLY / silent — just go back to listening
        logger.info(f"🤫 Silent reply for {call_sid}, redirecting to listen")
        try:
            client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            client.calls(call_sid).update(
                url=f"https://{PUBLIC_HOST}/voice",
                method="POST"
            )
        except Exception as e:
            logger.error(f"Failed to redirect call {call_sid}: {e}")
        return

    agent_response = sanitize_for_speech(agent_response)

    # Store the response
    pending_responses[call_sid] = agent_response
    logger.info(f"📦 Stored response for {call_sid}, redirecting call")

    # Redirect the active call to pick up the response
    try:
        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.calls(call_sid).update(
            url=f"https://{PUBLIC_HOST}/voice/deliver",
            method="POST"
        )
    except Exception as e:
        logger.warning(f"Redirect failed for {call_sid} (caller may have hung up): {e}")
        pending_responses.pop(call_sid, None)


# ── Twilio Endpoints ───────────────────────────────────────

@app.post("/voice")
async def handle_call(request: Request):
    """Entry point — greet caller and start listening."""
    response = VoiceResponse()
    response.say("Connected to Theo. Go ahead.", voice="alice")

    gather = Gather(
        input="speech",
        action=f"https://{PUBLIC_HOST}/voice/respond",
        method="POST",
        speech_timeout="auto",
        language="en-US",
    )
    response.append(gather)

    response.say("I didn't catch that. Try again.", voice="alice")
    response.redirect(f"https://{PUBLIC_HOST}/voice")

    logger.info("📞 Incoming call — listening for speech")
    return PlainTextResponse(str(response), media_type="text/xml")


@app.post("/voice/respond")
async def handle_speech(request: Request):
    """Receive transcription → return thinking tone immediately → process in background."""
    try:
        form = await request.form()
    except Exception as e:
        logger.warning(f"Form parse error: {e}")
        response = VoiceResponse()
        response.redirect(f"https://{PUBLIC_HOST}/voice")
        return PlainTextResponse(str(response), media_type="text/xml")

    speech_result = form.get("SpeechResult", "")
    confidence = form.get("Confidence", "0")
    call_sid = form.get("CallSid", "")

    logger.info(f"🎤 Speech: \"{speech_result}\" (confidence: {confidence}, call: {call_sid})")

    response = VoiceResponse()

    if not speech_result:
        response.say("I didn't catch that.", voice="alice")
        response.redirect(f"https://{PUBLIC_HOST}/voice")
        return PlainTextResponse(str(response), media_type="text/xml")

    # Kick off background processing — no timeout risk
    asyncio.create_task(process_and_redirect(call_sid, speech_result))

    # Return immediately with thinking tone loop + pause fallback
    # The pause keeps the call alive if the audio fails to load
    response.play(f"https://{PUBLIC_HOST}/assets/thinking-tone.mp3", loop=0)
    response.pause(length=60)

    logger.info(f"⏳ Thinking tone playing for {call_sid}")
    return PlainTextResponse(str(response), media_type="text/xml")


@app.post("/voice/deliver")
async def deliver_response(request: Request):
    """Called by Twilio after redirect — delivers the stored agent response."""
    try:
        form = await request.form()
    except Exception:
        form = {}

    call_sid = form.get("CallSid", "") if hasattr(form, 'get') else ""

    agent_response = pending_responses.pop(call_sid, None)
    if not agent_response:
        agent_response = "Sorry, I lost my train of thought."

    response = VoiceResponse()
    response.say(agent_response, voice="alice")

    # Loop — listen for next input
    gather = Gather(
        input="speech",
        action=f"https://{PUBLIC_HOST}/voice/respond",
        method="POST",
        speech_timeout="auto",
        language="en-US",
    )
    response.append(gather)

    response.say("Are you still there?", voice="alice")
    response.redirect(f"https://{PUBLIC_HOST}/voice")

    logger.info(f"🔊 Delivered response to {call_sid}")
    return PlainTextResponse(str(response), media_type="text/xml")


@app.get("/assets/{filename}")
async def serve_asset(filename: str):
    """Serve static assets (thinking tone, etc.)."""
    filepath = os.path.join(ASSETS_DIR, filename)
    if os.path.exists(filepath):
        return FileResponse(filepath, media_type="audio/mpeg")
    return PlainTextResponse("Not found", status_code=404)


@app.post("/voice/outbound")
async def handle_outbound(request: Request):
    """Entry point for outbound calls."""
    return await handle_call(request)


# ── Outbound Call ──────────────────────────────────────────

def make_call(to_number):
    """Initiate a call to someone."""
    client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    call = client.calls.create(
        to=to_number,
        from_=TWILIO_PHONE,
        url=f"https://{PUBLIC_HOST}/voice/outbound",
    )
    logger.info(f"📞 Calling {to_number}: {call.sid}")
    return call.sid


# ── Entry Point ────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description='Twilio Voice Adapter')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--call', help='Phone number to call')
    args = parser.parse_args()

    if args.call:
        make_call(args.call)
    else:
        logger.info(f"Twilio voice server on {args.host}:{args.port}")
        logger.info(f"Webhook: https://{PUBLIC_HOST}/voice")
        uvicorn.run(app, host=args.host, port=args.port)
