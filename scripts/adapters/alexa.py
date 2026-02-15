"""
Alexa Voice Adapter
Handles audio I/O with a physical Echo device via speakers/microphone.
"""
import time
import logging
import re
from typing import Optional
from ..voice_core.tts import TTSEngine
from ..voice_core.stt import STTEngine
from ..voice_core.conversation import ConversationManager

logger = logging.getLogger(__name__)


class AlexaAdapter:
    WAKE_WORD = "Alexa"

    def __init__(self, tts_engine=None, stt_engine=None, conversation=None,
                 wake_word="Alexa", mic_mute_delay=0.8):
        """
        Args:
            tts_engine: TTSEngine instance (created if None)
            stt_engine: STTEngine instance (created if None)
            conversation: ConversationManager instance (created if None)
            wake_word: Wake word for the device (default "Alexa")
            mic_mute_delay: Extra delay after speaking to avoid echo
        """
        self.wake_word = wake_word
        self.tts = tts_engine or TTSEngine(rate=160, volume=0.9)
        self.stt = stt_engine or STTEngine()
        self.conversation = conversation or ConversationManager(
            self.tts, self.stt, mic_mute_delay=mic_mute_delay
        )

    def command(self, text, listen=True):
        """
        Send a command to Alexa and optionally listen for response.

        Args:
            text: Command text (wake word is prepended automatically)
            listen: Whether to listen for a response

        Returns:
            str or None: Alexa's response text
        """
        full_command = f"{self.wake_word}, {text}"
        logger.info(f"Alexa command: {full_command}")

        if listen:
            # Alexa needs a bit more time to process shopping queries
            response = self.conversation.speak_and_listen(
                full_command,
            )
            return response
        else:
            self.conversation.say(full_command)
            return None

    def ask(self, question):
        """Ask Alexa a question and get the response."""
        return self.command(question, listen=True)

    def tell(self, statement):
        """Tell Alexa something without expecting a response."""
        return self.command(statement, listen=False)

    def confirm(self):
        """Say 'yes' to Alexa (e.g., confirming a purchase)."""
        return self.conversation.speak_and_listen("Yes")

    def deny(self):
        """Say 'no' to Alexa."""
        return self.conversation.speak_and_listen("No")

    def test_connection(self):
        """
        Test that we can communicate with Alexa.
        Asks for the time and checks we get a response.

        Returns:
            dict: {success: bool, response: str, latency_ms: int}
        """
        start = time.time()
        response = self.ask("what time is it")
        latency = int((time.time() - start) * 1000)

        success = response is not None and len(response) > 0
        return {
            'success': success,
            'response': response,
            'latency_ms': latency
        }

    def get_transcript(self):
        """Get conversation transcript."""
        return self.conversation.get_transcript()

    def reset(self):
        """Reset conversation state."""
        self.conversation.reset()


if __name__ == '__main__':
    print("AlexaAdapter loaded. Requires Echo device for testing.")
    print(f"Wake word: {AlexaAdapter.WAKE_WORD}")
