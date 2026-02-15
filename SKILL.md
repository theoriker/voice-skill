# Voice Module Skill

## Description
Modular voice conversation system for interacting with voice assistants (Alexa, etc.)
and conducting voice-driven workflows (shopping, commands, phone calls in future).

## Architecture
- **voice_core/** — TTS, STT, and conversation state machine (transport-agnostic)
- **adapters/** — Device-specific adapters (Alexa, future: phone/SIP)
- **shopping/** — Amazon voice shopping logic (ordering, price checks, confirmations)
- **config/** — YAML configuration
- **logs/** — Purchase audit trail (auto-created)

## Requirements
- macOS with speakers + microphone (Mac mini audio or USB audio device)
- Python 3.10+
- `pip3 install pyttsx3 pyaudio webrtcvad numpy mlx-whisper`
- `brew install portaudio` (for pyaudio)
- Physical Echo device for Alexa adapter (speaker output → Echo mic, Echo speaker → Mac mic)

## CLI Usage

```bash
# Navigate to skill directory
cd ~/.openclaw/workspace/skills/voice

# TTS
python3 scripts/voice_cli.py tts "Hello from Theo"
python3 scripts/voice_cli.py tts-test
python3 scripts/voice_cli.py voices

# STT
python3 scripts/voice_cli.py stt-test
python3 scripts/voice_cli.py stt-devices

# Alexa (requires Echo device)
python3 scripts/voice_cli.py alexa-test
python3 scripts/voice_cli.py alexa-ask "what time is it"

# Shopping (requires Echo + Amazon account linked)
python3 scripts/voice_cli.py shop "paper towels" --max-price 15
python3 scripts/voice_cli.py shop "windshield washer fluid" --auto-confirm
```

## Agent Usage

When asked to order something via voice/Alexa:
1. Run the `shop` command with appropriate `--max-price`
2. Default: requires human approval for orders > $50
3. Auto-confirm for orders < $15 (configurable in voice-config.yaml)
4. All purchase attempts are logged to `logs/purchases.jsonl`

When asked to test voice:
1. `tts-test` — confirms speaker output works
2. `stt-test` — confirms microphone + transcription works
3. `alexa-test` — confirms end-to-end Alexa communication

## Configuration
Edit `config/voice-config.yaml` to adjust:
- TTS rate/volume
- STT model and VAD sensitivity
- Shopping price limits
- Conversation retry/timeout settings

## Audio Setup for Alexa
The Mac mini's speaker output needs to reach the Echo's microphone, and the Echo's
speaker output needs to reach the Mac mini's microphone. Options:
1. **Proximity**: Place Echo next to Mac mini speakers/mic (simplest, works for testing)
2. **Audio cable**: 3.5mm line-out → Echo aux-in (one direction only)
3. **Virtual audio routing**: Loopback + aggregate devices (advanced, lower latency)

## Safety
- All purchases are logged with timestamps
- Price caps prevent runaway spending
- Human approval required above configurable threshold
- Deny (say "no") is automatic for unexpected responses
