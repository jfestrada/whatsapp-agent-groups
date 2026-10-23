import os
import tempfile
import unittest
from pathlib import Path

import transcribe


class TranscribeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data = root / "data"
        self.data.mkdir()
        self.cache = root / "cache" / "transcriptions.db"
        os.environ["WHISPER_DATA_ROOT"] = str(self.data)
        os.environ["WHISPER_CACHE_PATH"] = str(self.cache)
        self.audio = self.data / "note.ogg"
        self.audio.write_bytes(b"same-audio-bytes")

    def tearDown(self):
        self.tmp.cleanup()

    def test_refuses_paths_outside_the_media_directory(self):
        outside = Path(self.tmp.name) / "outside.ogg"
        outside.write_bytes(b"secret")
        for candidate in (str(outside), "note.ogg", str(self.data / "missing.ogg")):
            with self.assertRaises(transcribe.TranscriptionError):
                transcribe.transcribe_file(candidate, transcriber=lambda _path: "nope")

        link = self.data / "escape.ogg"
        link.symlink_to(outside)
        with self.assertRaises(transcribe.TranscriptionError):
            transcribe.transcribe_file(str(link), transcriber=lambda _path: "nope")

    def test_cache_skips_the_model_for_the_same_file(self):
        calls = []

        def fake_model(path: Path) -> str:
            calls.append(path)
            return "  hola   grupo  "

        first = transcribe.transcribe_file(str(self.audio), transcriber=fake_model)
        second = transcribe.transcribe_file(str(self.audio), transcriber=fake_model)
        self.assertEqual(first, "hola grupo")
        self.assertEqual(second, "hola grupo")
        self.assertEqual(len(calls), 1)

        copy = self.data / "copy.ogg"
        copy.write_bytes(self.audio.read_bytes())
        third = transcribe.transcribe_file(str(copy), transcriber=fake_model)
        self.assertEqual(third, "hola grupo")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
