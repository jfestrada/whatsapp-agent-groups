import os
import unittest
from datetime import datetime
from unittest.mock import patch

import whatsapp


def audio_message(content="", media_type="audio"):
    return whatsapp.Message(
        timestamp=datetime(2026, 10, 23, 12, 0, 0),
        sender="666@s.whatsapp.net",
        content=content,
        is_from_me=True,
        chat_jid="111@g.us",
        id="audio-1",
        chat_name="Alpha",
        media_type=media_type,
    )


class FormatAudioTests(unittest.TestCase):
    def setUp(self):
        self.previous = os.environ.get("WHISPER_MCP_URL")
        os.environ["WHISPER_MCP_URL"] = "http://whisper-mcp:8000/mcp"

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("WHISPER_MCP_URL", None)
        else:
            os.environ["WHISPER_MCP_URL"] = self.previous

    def test_allowed_audio_becomes_the_transcript(self):
        message = audio_message()
        with patch.object(whatsapp, "download_media", return_value="/data/note.ogg") as download, \
                patch.object(whatsapp, "request_transcript", return_value="hola grupo") as whisper:
            text = whatsapp.format_message(message)
        download.assert_called_once_with("audio-1", "111@g.us")
        whisper.assert_called_once_with("/data/note.ogg")
        self.assertIn("hola grupo", text)
        self.assertNotIn("[audio", text)

    def test_failed_transcription_keeps_the_placeholder(self):
        message = audio_message()
        with patch.object(whatsapp, "download_media", return_value="/data/note.ogg"), \
                patch.object(whatsapp, "request_transcript", side_effect=whatsapp.WhisperError("down")):
            text = whatsapp.format_message(message)
        self.assertIn("[audio - Message ID: audio-1 - Chat JID: 111@g.us]", text)
        self.assertNotIn("hola grupo", text)

    def test_text_row_is_unchanged(self):
        message = audio_message(content="alpha-secret", media_type=None)
        with patch.object(whatsapp, "download_media") as download, \
                patch.object(whatsapp, "request_transcript") as whisper:
            text = whatsapp.format_message(message)
        download.assert_not_called()
        whisper.assert_not_called()
        self.assertIn("alpha-secret", text)
        self.assertNotIn("[audio", text)

    def test_whisper_url_unset_keeps_the_placeholder(self):
        os.environ.pop("WHISPER_MCP_URL", None)
        message = audio_message()
        with patch.object(whatsapp, "download_media") as download:
            text = whatsapp.format_message(message)
        download.assert_not_called()
        self.assertIn("[audio - Message ID: audio-1 - Chat JID: 111@g.us]", text)


if __name__ == "__main__":
    unittest.main()
