"""
Conversation State Machine
Manages multi-turn voice dialogs with external systems.
"""
import time
import json
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Callable

logger = logging.getLogger(__name__)


class ConversationState(Enum):
    IDLE = "idle"
    SPEAKING = "speaking"
    LISTENING = "listening"
    PROCESSING = "processing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass
class ConversationTurn:
    speaker: str  # "agent" or "device"
    text: str
    timestamp: float = field(default_factory=time.time)
    state: str = ""


@dataclass
class ConversationContext:
    """Tracks context for a multi-turn conversation."""
    product_name: Optional[str] = None
    product_offered: Optional[str] = None
    price_offered: Optional[float] = None
    confirmed: Optional[bool] = None
    order_id: Optional[str] = None
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)


class ConversationManager:
    def __init__(self, tts_engine, stt_engine, max_retries=3,
                 listen_timeout=10, retry_delay=2.0, mic_mute_delay=0.5):
        """
        Args:
            tts_engine: TTSEngine instance
            stt_engine: STTEngine instance
            max_retries: Max retries on failed turns
            listen_timeout: Seconds to wait for response
            retry_delay: Seconds between retries
            mic_mute_delay: Seconds to wait after speaking before listening
        """
        self.tts = tts_engine
        self.stt = stt_engine
        self.max_retries = max_retries
        self.listen_timeout = listen_timeout
        self.retry_delay = retry_delay
        self.mic_mute_delay = mic_mute_delay

        self.state = ConversationState.IDLE
        self.transcript = []
        self.context = ConversationContext()

    def speak_and_listen(self, text, expected_response=None):
        """
        Speak text, then listen for a response.

        Args:
            text: What to say
            expected_response: Optional callback(response_text) -> bool
                             to validate the response

        Returns:
            str or None: Response text, or None if no response
        """
        # Speak
        self.state = ConversationState.SPEAKING
        self._log_turn("agent", text)
        logger.info(f"Speaking: {text}")
        self.tts.speak(text, block=True)

        # Brief pause to avoid echo
        time.sleep(self.mic_mute_delay)

        # Listen
        self.state = ConversationState.LISTENING
        logger.info("Listening for response...")

        for attempt in range(self.max_retries):
            response = self.stt.listen_and_transcribe(
                max_duration=self.listen_timeout,
                pre_speech_timeout=self.listen_timeout
            )

            if response:
                self._log_turn("device", response)
                logger.info(f"Heard: {response}")

                # Validate if needed
                if expected_response and not expected_response(response):
                    logger.warning(f"Unexpected response (attempt {attempt+1}): {response}")
                    if attempt < self.max_retries - 1:
                        time.sleep(self.retry_delay)
                        continue

                self.state = ConversationState.PROCESSING
                return response

            logger.warning(f"No response (attempt {attempt+1}/{self.max_retries})")
            if attempt < self.max_retries - 1:
                time.sleep(self.retry_delay)

        self.state = ConversationState.ERROR
        return None

    def say(self, text):
        """Speak without listening for a response."""
        self.state = ConversationState.SPEAKING
        self._log_turn("agent", text)
        self.tts.speak(text, block=True)
        time.sleep(self.mic_mute_delay)
        self.state = ConversationState.IDLE

    def listen(self):
        """Listen without speaking first."""
        self.state = ConversationState.LISTENING
        response = self.stt.listen_and_transcribe(
            max_duration=self.listen_timeout,
            pre_speech_timeout=self.listen_timeout
        )
        if response:
            self._log_turn("device", response)
            self.state = ConversationState.PROCESSING
        else:
            self.state = ConversationState.IDLE
        return response

    def confirm(self):
        """Say 'yes' — for confirming Alexa purchases etc."""
        return self.speak_and_listen("Yes")

    def deny(self):
        """Say 'no' — for declining."""
        return self.speak_and_listen("No")

    def run_flow(self, steps):
        """
        Run a multi-step conversation flow.

        Args:
            steps: List of dicts, each with:
                - 'say': text to speak
                - 'expect': optional validation callback
                - 'on_response': optional callback(response) -> next action
                - 'max_retries': optional override

        Returns:
            list: All responses collected
        """
        responses = []
        for i, step in enumerate(steps):
            text = step.get('say', '')
            expect = step.get('expect', None)
            on_response = step.get('on_response', None)

            response = self.speak_and_listen(text, expected_response=expect)
            responses.append(response)

            if response is None:
                self.context.error = f"No response at step {i}"
                self.state = ConversationState.ERROR
                break

            if on_response:
                action = on_response(response)
                if action == 'stop':
                    break
                elif action == 'retry':
                    # Re-run this step
                    response = self.speak_and_listen(text, expected_response=expect)
                    responses[-1] = response

        self.state = ConversationState.COMPLETE
        return responses

    def _log_turn(self, speaker, text):
        """Log a conversation turn."""
        turn = ConversationTurn(
            speaker=speaker,
            text=text,
            state=self.state.value
        )
        self.transcript.append(turn)

    def get_transcript(self):
        """Get full conversation transcript."""
        return [
            {
                'speaker': t.speaker,
                'text': t.text,
                'timestamp': t.timestamp,
                'state': t.state
            }
            for t in self.transcript
        ]

    def save_transcript(self, filepath):
        """Save transcript to JSON file."""
        with open(filepath, 'w') as f:
            json.dump(self.get_transcript(), f, indent=2)

    def reset(self):
        """Reset conversation state."""
        self.state = ConversationState.IDLE
        self.transcript = []
        self.context = ConversationContext()


if __name__ == '__main__':
    print("ConversationManager loaded successfully.")
    print("States:", [s.value for s in ConversationState])
