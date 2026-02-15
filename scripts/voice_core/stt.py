"""
Speech-to-Text Engine
Captures audio from microphone and transcribes using Whisper.
Supports both MLX Whisper (Apple Silicon) and OpenAI Whisper.
"""
import subprocess
import tempfile
import os
import json
import time
import numpy as np

try:
    import pyaudio
    HAS_PYAUDIO = True
except ImportError:
    HAS_PYAUDIO = False

try:
    import webrtcvad
    HAS_VAD = True
except ImportError:
    HAS_VAD = False


class STTEngine:
    # Audio settings
    RATE = 16000
    CHANNELS = 1
    CHUNK = 480  # 30ms at 16kHz (required for webrtcvad)
    FORMAT = pyaudio.paInt16 if HAS_PYAUDIO else None

    def __init__(self, whisper_model="mlx-community/whisper-small-mlx",
                 use_mlx=True, vad_aggressiveness=2, input_device=None):
        """
        Args:
            whisper_model: Model name for transcription
            use_mlx: Use MLX Whisper (True) or OpenAI Whisper API (False)
            vad_aggressiveness: 0-3, higher = more aggressive filtering (default 2)
            input_device: PyAudio device index (None = default)
        """
        self.whisper_model = whisper_model
        self.use_mlx = use_mlx
        self.input_device = input_device

        if HAS_VAD:
            self.vad = webrtcvad.Vad(vad_aggressiveness)
        else:
            self.vad = None

        if HAS_PYAUDIO:
            self.audio = pyaudio.PyAudio()
        else:
            self.audio = None

    def list_input_devices(self):
        """List available audio input devices."""
        if not self.audio:
            return []
        devices = []
        for i in range(self.audio.get_device_count()):
            info = self.audio.get_device_info_by_index(i)
            if info['maxInputChannels'] > 0:
                devices.append({
                    'index': i,
                    'name': info['name'],
                    'channels': info['maxInputChannels'],
                    'rate': int(info['defaultSampleRate'])
                })
        return devices

    def record_until_silence(self, min_duration=1.0, max_duration=15.0,
                              silence_threshold=1.5, pre_speech_timeout=10.0):
        """
        Record audio, stopping after silence is detected.

        Args:
            min_duration: Minimum recording time in seconds
            max_duration: Maximum recording time in seconds
            silence_threshold: Seconds of silence before stopping
            pre_speech_timeout: Max seconds to wait for speech to begin

        Returns:
            bytes: Raw audio data (16-bit PCM, 16kHz, mono)
        """
        if not self.audio:
            raise RuntimeError("PyAudio not available")

        stream_kwargs = {
            'format': self.FORMAT,
            'channels': self.CHANNELS,
            'rate': self.RATE,
            'input': True,
            'frames_per_buffer': self.CHUNK,
        }
        if self.input_device is not None:
            stream_kwargs['input_device_index'] = self.input_device

        stream = self.audio.open(**stream_kwargs)
        frames = []
        speech_detected = False
        silence_start = None
        start_time = time.time()

        try:
            while True:
                elapsed = time.time() - start_time
                data = stream.read(self.CHUNK, exception_on_overflow=False)
                frames.append(data)

                # Check for speech using VAD
                is_speech = self._is_speech(data) if self.vad else self._is_loud(data)

                if is_speech:
                    speech_detected = True
                    silence_start = None
                elif speech_detected:
                    if silence_start is None:
                        silence_start = time.time()
                    elif time.time() - silence_start > silence_threshold:
                        if elapsed >= min_duration:
                            break  # Speech ended

                # Timeout checks
                if not speech_detected and elapsed > pre_speech_timeout:
                    return None  # No speech detected
                if elapsed > max_duration:
                    break  # Max duration hit

        finally:
            stream.stop_stream()
            stream.close()

        return b''.join(frames)

    def _is_speech(self, audio_chunk):
        """Use WebRTC VAD to detect speech."""
        try:
            return self.vad.is_speech(audio_chunk, self.RATE)
        except Exception:
            return self._is_loud(audio_chunk)

    def _is_loud(self, audio_chunk, threshold=150):
        """Simple amplitude-based speech detection fallback."""
        audio_array = np.frombuffer(audio_chunk, dtype=np.int16)
        return np.abs(audio_array).mean() > threshold

    def transcribe(self, audio_data=None, audio_file=None):
        """
        Transcribe audio to text.

        Args:
            audio_data: Raw audio bytes (16-bit PCM, 16kHz, mono)
            audio_file: Path to audio file

        Returns:
            str: Transcribed text
        """
        if audio_data is not None:
            # Save to temp WAV file
            audio_file = self._save_wav(audio_data)
            cleanup = True
        else:
            cleanup = False

        try:
            if self.use_mlx:
                return self._transcribe_mlx(audio_file)
            else:
                return self._transcribe_whisper_api(audio_file)
        finally:
            if cleanup and audio_file:
                os.unlink(audio_file)

    def _save_wav(self, audio_data):
        """Save raw audio data as WAV file."""
        import wave
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            tmpfile = f.name
        with wave.open(tmpfile, 'wb') as wf:
            wf.setnchannels(self.CHANNELS)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(self.RATE)
            wf.writeframes(audio_data)
        return tmpfile

    def _transcribe_mlx(self, audio_file):
        """Transcribe using MLX Whisper (Apple Silicon optimized)."""
        try:
            import mlx_whisper
            result = mlx_whisper.transcribe(
                audio_file,
                path_or_hf_repo=self.whisper_model
            )
            return result.get('text', '').strip()
        except ImportError:
            # Fall back to CLI
            result = subprocess.run(
                ['python3', '-m', 'mlx_whisper', audio_file,
                 '--model', self.whisper_model],
                capture_output=True, text=True
            )
            return result.stdout.strip()

    def _transcribe_whisper_api(self, audio_file):
        """Transcribe using OpenAI Whisper API."""
        api_key = os.environ.get('OPENAI_API_KEY', '')
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")

        result = subprocess.run([
            'curl', '-s', 'https://api.openai.com/v1/audio/transcriptions',
            '-H', f'Authorization: Bearer {api_key}',
            '-F', f'file=@{audio_file}',
            '-F', 'model=whisper-1',
        ], capture_output=True, text=True)

        data = json.loads(result.stdout)
        return data.get('text', '').strip()

    def listen_and_transcribe(self, **kwargs):
        """
        Convenience method: record audio then transcribe.
        Returns:
            str or None: Transcribed text, or None if no speech detected
        """
        audio = self.record_until_silence(**kwargs)
        if audio is None:
            return None
        return self.transcribe(audio_data=audio)

    def cleanup(self):
        """Release audio resources."""
        if self.audio:
            self.audio.terminate()


if __name__ == '__main__':
    stt = STTEngine()
    print("Input devices:")
    for d in stt.list_input_devices():
        print(f"  [{d['index']}] {d['name']}")
    print("\nListening for 5 seconds...")
    text = stt.listen_and_transcribe(max_duration=5, pre_speech_timeout=5)
    if text:
        print(f"Heard: {text}")
    else:
        print("No speech detected.")
    stt.cleanup()
