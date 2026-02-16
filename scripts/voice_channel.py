#!/usr/bin/env python3
"""
Voice Channel for OpenClaw
Full-duplex voice ↔ gateway bridge.

Inbound:  continuous mic → VAD → Whisper STT → chat.send
Outbound: chat events → macOS `say` TTS → speakers
"""
import sys, os, json, time, uuid, base64, threading, queue
import signal as sig_module, argparse, logging, subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.voice_core.stt import STTEngine

import websocket
from cryptography.hazmat.primitives.serialization import load_pem_private_key

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s')
logger = logging.getLogger('voice')


class DeviceAuth:
    """Ed25519 device auth for OpenClaw gateway."""
    def __init__(self, identity_path=None):
        path = identity_path or os.path.expanduser("~/.openclaw/identity/device.json")
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
        self.token = tok.get("token", "")
        self.scopes = tok.get("scopes", ["operator.admin"])

    def sign(self, payload):
        sig = self.private_key.sign(payload.encode())
        return base64.urlsafe_b64encode(sig).rstrip(b"=").decode()

    def connect_params(self, client_id, mode, role, nonce=None):
        ts = int(time.time() * 1000)
        ver = "v2" if nonce else "v1"
        parts = [ver, self.device_id, client_id, mode, role,
                 ",".join(self.scopes), str(ts), self.token]
        if nonce: parts.append(nonce)
        sig = self.sign("|".join(parts))
        dev = {"id": self.device_id, "publicKey": self.pub_b64url,
               "signature": sig, "signedAt": ts}
        if nonce: dev["nonce"] = nonce
        return {"auth": {"token": self.token}, "role": role,
                "scopes": self.scopes, "device": dev}


class VoiceChannel:
    def __init__(self, gateway_url="ws://localhost:18789",
                 session_key=None, agent="main",
                 mic_device=None, speaker_device=None,
                 tts_voice="Samantha", tts_rate=180,
                 whisper_model="mlx-community/whisper-small-mlx",
                 vad_aggressiveness=2, silence_threshold=1.5):

        self.gateway_url = gateway_url
        self.session_key = session_key or f"agent:{agent}:voice"
        self.device_auth = DeviceAuth()

        # STT
        self.stt = STTEngine(whisper_model=whisper_model, input_device=mic_device,
                             vad_aggressiveness=vad_aggressiveness)
        self.silence_threshold = silence_threshold

        # TTS config (macOS `say`)
        self.speaker_device = speaker_device
        self.tts_voice = tts_voice
        self.tts_rate = tts_rate

        # State
        self.ws = None
        self.connected = False
        self.running = False
        self.speaking = False
        self.tts_queue = queue.Queue()
        self.seen_seqs = set()  # deduplicate events
        self.req_counter = 0
        self.lock = threading.Lock()

    def start(self):
        self.running = True
        logger.info(f"Session: {self.session_key}")

        threading.Thread(target=self._ws_loop, daemon=True).start()
        for _ in range(50):
            if self.connected: break
            time.sleep(0.1)
        if not self.connected:
            logger.error("Gateway connection failed")
            self.running = False
            return False

        threading.Thread(target=self._tts_loop, daemon=True).start()
        threading.Thread(target=self._stt_loop, daemon=True).start()
        logger.info("Full duplex active")
        return True

    def stop(self):
        self.running = False
        try: self.ws.close()
        except: pass
        self.stt.cleanup()

    # ── WebSocket ──────────────────────────────────────────

    def _ws_loop(self):
        while self.running:
            try:
                self.ws = websocket.WebSocketApp(
                    self.gateway_url,
                    on_open=lambda ws: self._send_connect(),
                    on_message=self._on_message,
                    on_error=lambda ws, e: logger.error(f"WS: {e}"),
                    on_close=lambda ws, c, r: setattr(self, 'connected', False),
                )
                self.ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.error(f"WS loop: {e}")
            if self.running:
                time.sleep(3)

    def _send_connect(self, nonce=None):
        cid = "c-nonce" if nonce else "c-init"
        auth = self.device_auth.connect_params("gateway-client", "backend", "operator", nonce)
        self.ws.send(json.dumps({
            "type": "req", "method": "connect", "id": cid,
            "params": {
                "minProtocol": 3, "maxProtocol": 3,
                "client": {"id": "gateway-client", "mode": "backend", "version": "1.0.0",
                           "platform": "voice-channel", "displayName": "Voice Channel",
                           "instanceId": f"v-{uuid.uuid4().hex[:6]}"},
                "caps": ["chat.events"],
                **auth,
            }
        }))

    def _on_message(self, ws, raw):
        frame = json.loads(raw)
        ft = frame.get("type")

        if ft == "event":
            ev = frame.get("event", "")
            pl = frame.get("payload", {})
            if ev == "connect.challenge":
                self._send_connect(nonce=pl.get("nonce"))
            elif ev == "chat":
                # Deduplicate by runId+seq
                key = (pl.get("runId", ""), pl.get("seq", 0))
                if key in self.seen_seqs: return
                self.seen_seqs.add(key)
                # Prune old keys (keep last 100)
                if len(self.seen_seqs) > 200:
                    self.seen_seqs = set(list(self.seen_seqs)[-100:])
                self._on_chat(pl)

        elif ft == "res":
            rid = frame.get("id", "")
            if frame.get("error"):
                logger.error(f"[{rid}] {frame['error'].get('message','?')}")
            elif rid == "c-nonce":
                self.connected = True
                logger.info("Gateway connected ✓")
            elif rid == "c-init" and not self.connected:
                # First connect OK (before nonce) — mark connected provisionally
                self.connected = True
                logger.info("Gateway connected ✓ (awaiting challenge)")

    def _on_chat(self, pl):
        state = pl.get("state", "")
        msg = pl.get("message", {})

        if state == "final":
            text = ""
            if isinstance(msg, dict):
                content = msg.get("content", [])
                if isinstance(content, list):
                    text = "".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
            text = text.strip()
            if text and not any(text.startswith(skip) for skip in ("NO_REPLY", "HEARTBEAT_OK", "NO_")):
                logger.info(f"🤖 {text[:150]}")
                self.tts_queue.put(text)

    # ── Chat Send ──────────────────────────────────────────

    def send_message(self, text):
        with self.lock:
            self.req_counter += 1
            rid = f"m-{self.req_counter}"
        self.ws.send(json.dumps({
            "type": "req", "method": "chat.send", "id": rid,
            "params": {"sessionKey": self.session_key, "message": text,
                       "idempotencyKey": str(uuid.uuid4())}
        }))
        logger.info(f"🎤 {text}")

    # ── TTS (macOS `say`) ──────────────────────────────────

    def _tts_loop(self):
        while self.running:
            try:
                text = self.tts_queue.get(timeout=1)
            except queue.Empty:
                continue
            self.speaking = True
            try:
                self._speak(text)
            except Exception as e:
                logger.error(f"TTS: {e}")
            finally:
                import time as _t; _t.sleep(1.0)  # post-TTS delay to prevent echo
                self.speaking = False

    def _speak(self, text):
        """Speak via macOS `say` → speaker device, then restore default."""
        restore_to = None
        if self.speaker_device:
            restore_to = subprocess.run(
                ['SwitchAudioSource', '-c'], capture_output=True, text=True
            ).stdout.strip()
            subprocess.run(['SwitchAudioSource', '-s', self.speaker_device],
                           capture_output=True)
        subprocess.run(['say', '-v', self.tts_voice, '-r', str(self.tts_rate), text],
                       check=True)
        if restore_to and restore_to != self.speaker_device:
            import time as _t; _t.sleep(0.3)
            subprocess.run(['SwitchAudioSource', '-s', restore_to],
                           capture_output=True)

    # ── STT (continuous listen) ────────────────────────────

    def _stt_loop(self):
        logger.info("Mic listening")
        while self.running:
            try:
                if self.speaking:
                    time.sleep(0.1)
                    continue

                audio = self.stt.record_until_silence(
                    min_duration=0.5, max_duration=30.0,
                    silence_threshold=self.silence_threshold,
                    pre_speech_timeout=600,
                )
                if audio is None or self.speaking:
                    continue

                text = self.stt.transcribe(audio_data=audio)
                if text and text.strip():
                    self.send_message(text.strip())
            except Exception as e:
                logger.error(f"STT: {e}")
                time.sleep(1)


def main():
    p = argparse.ArgumentParser(description='OpenClaw Voice Channel')
    p.add_argument('--gateway', default='ws://localhost:18789')
    p.add_argument('--session', help='Session key')
    p.add_argument('--agent', default='main')
    p.add_argument('--mic', type=int, help='Mic device index')
    p.add_argument('--speaker', help='Speaker device name')
    p.add_argument('--voice', default='Samantha', help='macOS TTS voice')
    p.add_argument('--rate', type=int, default=180, help='TTS words/min')
    p.add_argument('--whisper-model', default='mlx-community/whisper-small-mlx')
    p.add_argument('--vad', type=int, default=2)
    p.add_argument('--debug', action='store_true')
    args = p.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    ch = VoiceChannel(
        gateway_url=args.gateway, session_key=args.session, agent=args.agent,
        mic_device=args.mic, speaker_device=args.speaker,
        tts_voice=args.voice, tts_rate=args.rate,
        whisper_model=args.whisper_model, vad_aggressiveness=args.vad,
    )

    sig_module.signal(sig_module.SIGINT, lambda s, f: (ch.stop(), sys.exit(0)))
    sig_module.signal(sig_module.SIGTERM, lambda s, f: (ch.stop(), sys.exit(0)))

    if ch.start():
        print("🎙️  Voice channel active. Speak freely. Ctrl+C to stop.")
        while ch.running:
            time.sleep(1)
    else:
        sys.exit(1)

if __name__ == '__main__':
    main()
