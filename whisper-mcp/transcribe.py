import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Callable, Optional


class TranscriptionError(Exception):
    """The audio file cannot be transcribed."""


def data_root() -> Path:
    return Path(os.environ.get("WHISPER_DATA_ROOT", "/data")).resolve()


def cache_path() -> Path:
    return Path(os.environ.get("WHISPER_CACHE_PATH", "/cache/transcriptions.db"))


def resolve_audio_path(path: str) -> Path:
    """Return the real file path, or refuse anything that is not a file under /data."""
    if not path or not os.path.isabs(path):
        raise TranscriptionError("Path is not an allowed media file")
    root = data_root()
    try:
        resolved = Path(path).resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise TranscriptionError("Path is not an allowed media file")
    if not resolved.is_file():
        raise TranscriptionError("Path is not an allowed media file")
    try:
        if os.path.commonpath([str(root), str(resolved)]) != str(root):
            raise TranscriptionError("Path is not an allowed media file")
    except ValueError:
        raise TranscriptionError("Path is not an allowed media file")
    return resolved


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _connect_cache() -> sqlite3.Connection:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5)
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS transcripts (
            sha256 TEXT PRIMARY KEY,
            text TEXT NOT NULL
        )
        """
    )
    return connection


def cache_get(digest: str) -> Optional[str]:
    connection = _connect_cache()
    try:
        row = connection.execute(
            "SELECT text FROM transcripts WHERE sha256 = ?",
            (digest,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return row[0]


def cache_put(digest: str, text: str) -> None:
    connection = _connect_cache()
    try:
        connection.execute(
            "INSERT OR REPLACE INTO transcripts (sha256, text) VALUES (?, ?)",
            (digest, text),
        )
        connection.commit()
    finally:
        connection.close()


_model = None
_model_key = None


def run_whisper(path: Path) -> str:
    global _model, _model_key
    from faster_whisper import WhisperModel

    model_name = os.environ.get("WHISPER_MODEL", "small").strip() or "small"
    device = os.environ.get("WHISPER_DEVICE", "cpu").strip() or "cpu"
    compute_type = os.environ.get("WHISPER_COMPUTE_TYPE", "int8").strip() or "int8"
    download_root = os.environ.get("WHISPER_MODEL_DIR", "/models").strip() or "/models"
    language = os.environ.get("WHISPER_LANGUAGE", "").strip() or None
    key = (model_name, device, compute_type, download_root)
    if _model is None or _model_key != key:
        _model = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
            download_root=download_root,
        )
        _model_key = key
    segments, _info = _model.transcribe(str(path), language=language)
    return " ".join(segment.text.strip() for segment in segments if segment.text).strip()


def transcribe_file(path: str, transcriber: Optional[Callable[[Path], str]] = None) -> str:
    resolved = resolve_audio_path(path)
    digest = file_sha256(resolved)
    cached = cache_get(digest)
    if cached is not None:
        return cached
    runner = transcriber or run_whisper
    text = runner(resolved)
    if not isinstance(text, str):
        raise TranscriptionError("Transcription failed")
    cleaned = " ".join(text.split())
    cache_put(digest, cleaned)
    return cleaned
