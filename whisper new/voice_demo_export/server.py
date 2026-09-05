#!/usr/bin/env python3
"""Flask backend for the voice transcription demo."""

import os

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

from transcribe import DEFAULT_MODEL, get_client, transcribe_audio

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(__name__, static_folder=STATIC_DIR)

ALLOWED_EXTENSIONS = {"wav", "mp3", "webm", "ogg", "flac", "m4a", "aac", "mp4", "oga"}
MAX_CONTENT_LENGTH = 25 * 1024 * 1024  # 25 MB
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

_client = None


def get_cached_client():
    global _client
    if _client is None:
        try:
            _client = get_client(allow_prompt=False)
        except RuntimeError as e:
            raise
    return _client


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/api/transcribe", methods=["POST"])
def transcribe_endpoint():
    try:
        client = get_cached_client()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    if "audio" not in request.files:
        return jsonify({"error": "No 'audio' file part in the request."}), 400

    file = request.files["audio"]
    if not file or not file.filename:
        return jsonify({"error": "Empty upload."}), 400

    ext = ""
    if "." in file.filename:
        ext = file.filename.rsplit(".", 1)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({
            "error": f"Unsupported file type '.{ext}'. "
                     f"Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}.",
        }), 400

    filename = secure_filename(file.filename) or "recording.webm"

    try:
        data = file.read()
        if not data:
            return jsonify({"error": "Uploaded audio is empty."}), 400
        text, elapsed = transcribe_audio(client, data, filename, DEFAULT_MODEL)
        return jsonify({"transcript": text, "elapsed": round(elapsed, 2)})
    except Exception as e:
        return jsonify({"error": f"Transcription failed: {e}"}), 500


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "Upload too large (max 25 MB)."}), 413


if __name__ == "__main__":
    print("Starting voice transcription demo on http://127.0.0.1:5000")
    print("GROQ_API_KEY set:", bool(os.environ.get("GROQ_API_KEY")))
    app.run(host="127.0.0.1", port=5000, debug=False)
