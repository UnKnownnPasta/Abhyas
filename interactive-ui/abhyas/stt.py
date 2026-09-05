# Speech to text: one recording in, one sentence out.
#
# This replaces a streaming transcriber (Deepgram over a websocket, interim
# results, an assembler that glued the pieces back together). Whisper is not a
# streaming model - it reads a whole clip - and pretending otherwise was most
# of the complexity in the old path. What went with it:
#
#   - no interim captions. The words appear when the clip is transcribed, not
#     as they are spoken. That is a real loss and the interface says
#     "Transcribing..." rather than showing a half sentence that will change.
#   - no keepalive, no CloseStream, no ten-second silence timeout, no
#     sentence-assembly rules about which event flushes what. A recording
#     either uploads or it doesn't.
#   - one key instead of two. Whisper is hosted by the same provider that
#     already reads the sentences, so ABHYAS_LLM_API_KEY covers both.
#
# Hand-rolled multipart over urllib rather than the vendor SDK, because
# voice.py already calls this same host that way and one endpoint does not
# justify a dependency that the offline path would then have to work around.

import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
import uuid

# Imported for its .env loading, which is what puts the key in the
# environment. Without it this module works when the server imports it and
# reports "no key" when run on its own, which is a confusing way to find out
# your key is fine.
from . import config as _config          # noqa: F401  (imported for effect)

# Its own name so the two hosted features can be keyed separately, but it
# falls back to the parser's key: both are Groq, and making someone paste the
# same key twice is a good way to have one of them silently unset.
API_KEY_ENV = "ABHYAS_STT_API_KEY"
FALLBACK_KEY_ENV = "ABHYAS_LLM_API_KEY"

BASE_URL = os.environ.get("ABHYAS_STT_BASE_URL", "https://api.groq.com/openai/v1")
MODEL = os.environ.get("ABHYAS_STT_MODEL", "whisper-large-v3")

# Generous, because it covers upload plus transcription of a clip that may be
# most of a minute. Short enough that a hung request doesn't leave the mic
# button dead with no explanation.
TIMEOUT_S = float(os.environ.get("ABHYAS_STT_TIMEOUT_S", "45"))

# English only. This junction's vocabulary is English and a model left to
# auto-detect will confidently translate an accented English sentence into
# something else, which reads as a transcription error nobody can debug.
LANGUAGE = os.environ.get("ABHYAS_STT_LANGUAGE", "en")

# What a browser's MediaRecorder actually produces, plus the formats someone
# might upload by hand. Checked here rather than left to the API so a bad
# format is a sentence, not an HTTP 400 relayed as-is.
ALLOWED_EXTENSIONS = ("wav", "mp3", "webm", "ogg", "oga", "flac", "m4a",
                      "aac", "mp4", "mpga")

# The API's own ceiling is larger; this is the point past which a browser
# recording is a mistake rather than a sentence.
MAX_BYTES = int(os.environ.get("ABHYAS_STT_MAX_BYTES", str(24 * 1024 * 1024)))

# Below this there is no audio in the file, only a container header. Uploading
# it wastes a round trip to be told nothing was said.
MIN_BYTES = 1024


class Unavailable(Exception):
    """No key, so there is no transcriber to call."""


class Rejected(Exception):
    """The recording itself is wrong - too big, too small, wrong format."""


def api_key():
    for name in (API_KEY_ENV, FALLBACK_KEY_ENV):
        key = os.environ.get(name)
        if key and key.strip():
            return key.strip()
    return None


def available():
    return api_key() is not None


def status():
    if not available():
        return {"backend": None, "model": None,
                "note": "Voice input is off. Set " + API_KEY_ENV + " (or "
                        + FALLBACK_KEY_ENV + ", which is the same provider) "
                        "to enable it."}
    return {"backend": "whisper", "model": MODEL, "base_url": BASE_URL,
            "streaming": False,
            "note": "The recording is uploaded to " + BASE_URL + " when you "
                    "stop speaking, and transcribed there. Nothing is sent "
                    "while you are still talking, so there are no live "
                    "captions - the sentence appears once."}


def extension_of(filename):
    name = (filename or "").strip().lower()
    return name.rsplit(".", 1)[1] if "." in name else ""


def check(audio, filename):
    """Everything we can tell about a recording without uploading it."""
    extension = extension_of(filename)
    if extension not in ALLOWED_EXTENSIONS:
        raise Rejected(
            "'" + (extension or "no extension") + "' is not an audio format "
            "this accepts. Allowed: " + ", ".join(ALLOWED_EXTENSIONS) + ".")
    if len(audio) > MAX_BYTES:
        raise Rejected(
            "That recording is " + str(round(len(audio) / 1e6, 1)) + " MB, over "
            "the " + str(round(MAX_BYTES / 1e6)) + " MB limit. Say it in a "
            "shorter sentence - this interface takes one instruction at a time "
            "anyway.")
    if len(audio) < MIN_BYTES:
        raise Rejected(
            "That recording is empty - it holds a container header and no "
            "audio. Usually the microphone never actually started; check it "
            "is not muted and try again.")
    return extension


def _multipart(fields, filename, audio):
    """Build one multipart/form-data body. Small enough to do by hand, and
    doing it by hand keeps the dependency list where it is."""
    boundary = "----abhyas" + uuid.uuid4().hex
    marker = ("--" + boundary).encode("ascii")
    parts = []
    for name, value in fields.items():
        parts += [marker,
                  ('Content-Disposition: form-data; name="' + name + '"'
                   ).encode("ascii"),
                  b"", str(value).encode("utf-8")]
    content_type = (mimetypes.guess_type(filename)[0]
                    or "application/octet-stream")
    parts += [marker,
              ('Content-Disposition: form-data; name="file"; filename="'
               + filename + '"').encode("utf-8"),
              ("Content-Type: " + content_type).encode("ascii"),
              b"", audio,
              ("--" + boundary + "--").encode("ascii"), b""]
    return b"\r\n".join(parts), "multipart/form-data; boundary=" + boundary


def transcribe(audio, filename="recording.webm", model=None):
    """Bytes in, (text, seconds) out. Raises rather than returning a guess."""
    key = api_key()
    if key is None:
        raise Unavailable("No speech-to-text key is set (" + API_KEY_ENV
                          + " or " + FALLBACK_KEY_ENV + ").")
    check(audio, filename)

    body, content_type = _multipart(
        {"model": model or MODEL, "language": LANGUAGE,
         "response_format": "json",
         # Whisper hallucinates fluent nonsense on silence and on audio it
         # cannot place. Telling it what this recording is about is the one
         # lever that reliably keeps it near the vocabulary the parser knows.
         "prompt": PROMPT,
         "temperature": "0"},
        filename, audio)

    request = urllib.request.Request(
        BASE_URL.rstrip("/") + "/audio/transcriptions", data=body,
        headers={"Content-Type": content_type,
                 "Authorization": "Bearer " + key,
                 "User-Agent": "abhyas-interactive-ui/1.0"})

    started = time.time()
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        payload = json.loads(response.read().decode("utf-8"))
    elapsed = time.time() - started

    text = (payload.get("text") or "").strip()
    return text, elapsed


# Domain words, so "CMH" doesn't come back as "see em aitch" and "two-wheeler"
# doesn't come back as "2 Wheeler Road". This biases the transcript towards
# the vocabulary nlu.py can actually place; it does not put words in anyone's
# mouth, and anything outside the schema still gets rejected downstream.
PROMPT = ("A traffic engineer speaking about one signalised junction in "
          "Bengaluru: CMH Road and 100 Feet Road, Indiranagar. Terms: green "
          "time, phase, north-south, east-west, approach, arm, queue, "
          "counterfactual, validation, baseline, veh/h, two-wheeler, "
          "auto-rickshaw, bus, HCV, truck, mode shift, access restriction, "
          "obstruction, cow, roadworks, stalled vehicle.")


def error_detail(exc):
    """Turn a failed call into something a person can act on."""
    if isinstance(exc, urllib.error.HTTPError):
        raw = exc.read().decode("utf-8", "replace")[:400]
        try:
            message = json.loads(raw).get("error", {}).get("message") or raw
        except (json.JSONDecodeError, AttributeError):
            message = raw
        if exc.code in (401, 403):
            return ("the transcriber refused the key (HTTP " + str(exc.code)
                    + "). Check " + API_KEY_ENV + " or " + FALLBACK_KEY_ENV
                    + ".")
        if exc.code == 429:
            return "the transcriber is rate-limiting this key. Try again."
        return "the transcriber returned HTTP " + str(exc.code) + ": " + message
    if isinstance(exc, (TimeoutError, urllib.error.URLError)):
        return ("the transcriber could not be reached within "
                + str(int(TIMEOUT_S)) + " s. The simulation is unaffected - "
                "type the instruction instead.")
    return type(exc).__name__ + ": " + str(exc)


if __name__ == "__main__":
    import sys
    print(json.dumps(status(), indent=2))
    if len(sys.argv) > 1:
        path = sys.argv[1]
        with open(path, "rb") as handle:
            data = handle.read()
        text, seconds = transcribe(data, os.path.basename(path))
        print(format(seconds, ".1f") + "s: " + text)
