"""
Text-to-Speech Engine
Converts text to audio output, directed at a specific audio device.
"""
import pyttsx3
import time
import subprocess
import tempfile
import os


class TTSEngine:
    def __init__(self, rate=175, volume=0.8, voice=None, output_device=None):
        """
        Args:
            rate: Words per minute (default 175)
            volume: 0.0 to 1.0 (default 0.8)
            voice: Voice ID string (None = system default)
            output_device: Audio device name/index (None = default)
        """
        self.rate = rate
        self.volume = volume
        self.voice = voice
        self.output_device = output_device
        self._init_engine()

    def _init_engine(self):
        self.engine = pyttsx3.init()
        self.engine.setProperty('rate', self.rate)
        self.engine.setProperty('volume', self.volume)
        if self.voice:
            self.engine.setProperty('voice', self.voice)

    def list_voices(self):
        """List available system voices."""
        voices = self.engine.getProperty('voices')
        return [{'id': v.id, 'name': v.name, 'languages': v.languages} for v in voices]

    def speak(self, text, block=True):
        """
        Speak text aloud.
        Args:
            text: String to speak
            block: Wait for speech to finish (default True)
        """
        self.engine.say(text)
        if block:
            self.engine.runAndWait()

    def speak_to_file(self, text, filepath):
        """Save speech as audio file (WAV/AIFF)."""
        self.engine.save_to_file(text, filepath)
        self.engine.runAndWait()

    def speak_via_afplay(self, text, device=None):
        """
        Speak by saving to temp file and playing via afplay.
        Allows targeting a specific audio device on macOS.
        """
        with tempfile.NamedTemporaryFile(suffix='.aiff', delete=False) as f:
            tmpfile = f.name
        try:
            self.engine.save_to_file(text, tmpfile)
            self.engine.runAndWait()
            cmd = ['afplay', tmpfile]
            if device:
                cmd.extend(['--audio-device', str(device)])
            subprocess.run(cmd, check=True)
        finally:
            os.unlink(tmpfile)

    @staticmethod
    def list_audio_devices():
        """List macOS audio output devices."""
        try:
            result = subprocess.run(
                ['system_profiler', 'SPAudioDataType', '-json'],
                capture_output=True, text=True
            )
            import json
            data = json.loads(result.stdout)
            devices = []
            for item in data.get('SPAudioDataType', []):
                items = item.get('_items', [])
                for d in items:
                    devices.append(d.get('_name', 'Unknown'))
            return devices
        except Exception:
            return []


if __name__ == '__main__':
    tts = TTSEngine()
    print("Available voices:")
    for v in tts.list_voices()[:5]:
        print(f"  {v['name']}")
    print("\nSpeaking test...")
    tts.speak("Hello, this is Theo. Voice module test successful.")
    print("Done.")
