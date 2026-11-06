"""Speech for the WhatsApp agent via Sarvam AI (Indian languages).

- Speech-to-text: POST https://api.sarvam.ai/speech-to-text (multipart `file`, `model`, `language_code=unknown` for auto-detect).
- Text-to-speech: POST https://api.sarvam.ai/text-to-speech (JSON `text`, `language_code`, `model`, `speaker`, `output_audio_codec`).
Both use the `api-subscription-key` header from SARVAM_API_KEY. Without a key, voice notes are kept as audio for the
clinician and replies are text only (the simulator can still use the browser's speech services).
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
import uuid
from typing import Any

BASE = "https://api.sarvam.ai"
LANG_CODES = {"en": "en-IN", "hi": "hi-IN", "te": "te-IN", "ta": "ta-IN", "kn": "kn-IN", "ml": "ml-IN", "mr": "mr-IN", "bn": "bn-IN"}
AUDIO_EXT = {"audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/ogg": ".ogg", "audio/webm": ".webm", "audio/mp4": ".m4a", "audio/aac": ".aac"}


def _key() -> str:
    return os.environ.get("SARVAM_API_KEY", "").strip()


def status() -> dict[str, Any]:
    return {"provider": "Sarvam AI" if _key() else None, "enabled": bool(_key()),
            "stt_model": os.environ.get("SARVAM_STT_MODEL", "saaras:v4"), "tts_model": os.environ.get("SARVAM_TTS_MODEL", "bulbul:v3"),
            "hint": None if _key() else "Set SARVAM_API_KEY in .env to transcribe WhatsApp voice notes and send spoken replies."}


def _request(path: str, data: bytes, content_type: str, timeout: float = 60) -> dict[str, Any]:
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"api-subscription-key": _key(), "Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            detail = error.read().decode(errors="replace")[:300]
        except Exception:
            detail = ""
        raise RuntimeError(f"Sarvam HTTP {error.code}: {detail or error.reason}") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"Cannot reach api.sarvam.ai ({error.reason})") from None


def short_lang(code: str | None) -> str | None:
    """'te-IN' -> 'te'."""
    if not code:
        return None
    return code.split("-")[0].lower()


def transcribe(data: bytes, content_type: str) -> dict[str, Any]:
    """Returns {"text", "lang"} (lang as 'te'/'hi'/'en'...). Raises RuntimeError when the service fails or no key is set."""
    if not _key():
        raise RuntimeError("SARVAM_API_KEY is not set")
    boundary = uuid.uuid4().hex
    ext = AUDIO_EXT.get(content_type.split(";")[0], ".ogg")
    fields = {"model": os.environ.get("SARVAM_STT_MODEL", "saaras:v4"), "language_code": "unknown"}
    body = b""
    for name, value in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"voice{ext}\"\r\n"
             f"Content-Type: {content_type.split(';')[0]}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    reply = _request("/speech-to-text", body, f"multipart/form-data; boundary={boundary}")
    return {"text": (reply.get("transcript") or "").strip(), "lang": short_lang(reply.get("language_code")),
            "language_code": reply.get("language_code")}


def synthesize(text: str, lang: str) -> tuple[bytes, str]:
    """Spoken version of `text` as MP3 (WhatsApp plays audio/mpeg). Raises RuntimeError on failure."""
    if not _key():
        raise RuntimeError("SARVAM_API_KEY is not set")
    clean = text.replace("*", "").strip()[:2400]
    body = {"text": clean, "language_code": LANG_CODES.get(lang, "en-IN"), "model": os.environ.get("SARVAM_TTS_MODEL", "bulbul:v3"),
            "speaker": os.environ.get("SARVAM_TTS_SPEAKER", "priya"), "output_audio_codec": "mp3", "pace": 0.95}
    try:
        reply = _request("/text-to-speech", json.dumps(body).encode(), "application/json")
    except RuntimeError as error:
        if "422" not in str(error) and "400" not in str(error):
            raise
        # Older API revisions name the field target_language_code and only return WAV.
        body["target_language_code"] = body.pop("language_code")
        body.pop("output_audio_codec", None)
        reply = _request("/text-to-speech", json.dumps(body).encode(), "application/json")
        audio = base64.b64decode((reply.get("audios") or [""])[0])
        return audio, "audio/wav"
    audio = base64.b64decode((reply.get("audios") or [""])[0])
    if not audio:
        raise RuntimeError("Sarvam returned no audio")
    return audio, "audio/mpeg"
