#!/usr/bin/env python3
"""
Voice Module CLI
Entry point for all voice operations.

Usage:
    python3 voice_cli.py tts "Hello world"
    python3 voice_cli.py tts-test
    python3 voice_cli.py stt-test
    python3 voice_cli.py stt-devices
    python3 voice_cli.py alexa test
    python3 voice_cli.py alexa ask "what time is it"
    python3 voice_cli.py alexa order "paper towels" [--max-price 10]
    python3 voice_cli.py shop "windshield washer fluid" [--max-price 20] [--auto-confirm]
"""
import sys
import os
import json
import argparse
import logging

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.voice_core.tts import TTSEngine
from scripts.voice_core.stt import STTEngine
from scripts.voice_core.conversation import ConversationManager
from scripts.adapters.alexa import AlexaAdapter
from scripts.shopping.amazon import AmazonShopper

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger('voice')


def load_config():
    """Load voice config from YAML."""
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'config', 'voice-config.yaml'
    )
    try:
        import yaml
        with open(config_path) as f:
            return yaml.safe_load(f)
    except ImportError:
        # Parse simple YAML manually
        config = {}
        try:
            with open(config_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and ':' in line:
                        # Very basic key: value parsing
                        pass
        except Exception:
            pass
        return config
    except Exception:
        return {}


def create_engines(config=None):
    """Create TTS and STT engines from config."""
    config = config or {}
    audio = config.get('audio', {})
    stt_cfg = config.get('stt', {})

    tts = TTSEngine(
        rate=audio.get('tts_rate', 160),
        volume=audio.get('tts_volume', 0.9)
    )
    stt = STTEngine(
        whisper_model=stt_cfg.get('model', 'mlx-community/whisper-small-mlx'),
        use_mlx=stt_cfg.get('engine', 'mlx') == 'mlx',
        vad_aggressiveness=stt_cfg.get('vad_aggressiveness', 2)
    )
    return tts, stt


def cmd_tts(args):
    """Speak text aloud."""
    tts = TTSEngine(rate=160, volume=0.9)
    text = ' '.join(args.text)
    tts.speak(text)
    print(f"Spoke: {text}")


def cmd_tts_test(args):
    """Quick TTS test."""
    tts = TTSEngine(rate=160, volume=0.9)
    tts.speak("Voice module online. All systems nominal.")
    print("TTS test complete.")


def cmd_stt_test(args):
    """Quick STT test — listen for 5 seconds."""
    stt = STTEngine()
    print("Listening for 5 seconds... speak now.")
    text = stt.listen_and_transcribe(max_duration=5, pre_speech_timeout=5)
    if text:
        print(f"Transcribed: {text}")
    else:
        print("No speech detected.")
    stt.cleanup()


def cmd_stt_devices(args):
    """List input audio devices."""
    stt = STTEngine()
    devices = stt.list_input_devices()
    if devices:
        print("Input devices:")
        for d in devices:
            print(f"  [{d['index']}] {d['name']} ({d['channels']}ch, {d['rate']}Hz)")
    else:
        print("No input devices found.")
    stt.cleanup()


def cmd_alexa_test(args):
    """Test Alexa connection by asking the time."""
    tts, stt = create_engines()
    alexa = AlexaAdapter(tts_engine=tts, stt_engine=stt)
    print("Testing Alexa connection (asking for the time)...")
    result = alexa.test_connection()
    print(json.dumps(result, indent=2))
    stt.cleanup()


def cmd_alexa_ask(args):
    """Ask Alexa a question."""
    tts, stt = create_engines()
    alexa = AlexaAdapter(tts_engine=tts, stt_engine=stt)
    question = ' '.join(args.question)
    print(f"Asking Alexa: {question}")
    response = alexa.ask(question)
    print(f"Response: {response}")
    stt.cleanup()


def cmd_shop(args):
    """Order a product via Alexa."""
    tts, stt = create_engines()
    alexa = AlexaAdapter(tts_engine=tts, stt_engine=stt)

    log_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'logs'
    )
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'purchases.jsonl')

    shopper = AmazonShopper(
        alexa_adapter=alexa,
        max_price=args.max_price,
        log_file=log_file
    )

    product = ' '.join(args.product)
    print(f"Ordering: {product} (max ${args.max_price:.2f})")
    result = shopper.order(
        product,
        max_price=args.max_price,
        auto_confirm=args.auto_confirm
    )

    print(f"\nResult:")
    print(f"  Success: {result.success}")
    if result.product:
        print(f"  Product: {result.product.name}")
        print(f"  Price: ${result.product.price}" if result.product.price else "  Price: unknown")
    if result.error:
        print(f"  Error: {result.error}")
    if result.order_placed:
        print(f"  ✅ Order placed!")

    stt.cleanup()


def cmd_voices(args):
    """List available TTS voices."""
    tts = TTSEngine()
    voices = tts.list_voices()
    for v in voices:
        print(f"  {v['name']}")
        print(f"    ID: {v['id']}")
        if v.get('languages'):
            print(f"    Languages: {v['languages']}")


def main():
    parser = argparse.ArgumentParser(description='Voice Module CLI')
    subparsers = parser.add_subparsers(dest='command', help='Command')

    # TTS commands
    tts_p = subparsers.add_parser('tts', help='Speak text')
    tts_p.add_argument('text', nargs='+', help='Text to speak')
    tts_p.set_defaults(func=cmd_tts)

    subparsers.add_parser('tts-test', help='Quick TTS test').set_defaults(func=cmd_tts_test)
    subparsers.add_parser('voices', help='List TTS voices').set_defaults(func=cmd_voices)

    # STT commands
    subparsers.add_parser('stt-test', help='Quick STT test (5s listen)').set_defaults(func=cmd_stt_test)
    subparsers.add_parser('stt-devices', help='List input devices').set_defaults(func=cmd_stt_devices)

    # Alexa commands
    alexa_test_p = subparsers.add_parser('alexa-test', help='Test Alexa connection')
    alexa_test_p.set_defaults(func=cmd_alexa_test)

    alexa_ask_p = subparsers.add_parser('alexa-ask', help='Ask Alexa a question')
    alexa_ask_p.add_argument('question', nargs='+', help='Question to ask')
    alexa_ask_p.set_defaults(func=cmd_alexa_ask)

    # Shopping
    shop_p = subparsers.add_parser('shop', help='Order product via Alexa')
    shop_p.add_argument('product', nargs='+', help='Product to order')
    shop_p.add_argument('--max-price', type=float, default=50.0, help='Max price (default $50)')
    shop_p.add_argument('--auto-confirm', action='store_true', help='Auto-confirm purchase')
    shop_p.set_defaults(func=cmd_shop)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == '__main__':
    main()
