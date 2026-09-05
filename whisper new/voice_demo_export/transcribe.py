#!/usr/bin/env python3
"""Voice transcription demo using Groq's Whisper API (English only)."""

import argparse
import ctypes
import os
import sys
import time

import numpy as np
import soundfile as sf
from dotenv import load_dotenv
from groq import Groq


load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


DEFAULT_MODEL = "whisper-large-v3"
SAMPLE_RATE = 16000
CHANNELS = 1


def get_api_key(allow_prompt=True):
    key = os.environ.get("GROQ_API_KEY")
    if key:
        return key.strip()
    if allow_prompt:
        key = input("GROQ_API_KEY not set. Paste your key: ").strip()
        if not key:
            sys.exit("No API key provided.")
        os.environ["GROQ_API_KEY"] = key
        return key
    raise RuntimeError("GROQ_API_KEY environment variable is not set")


def get_client(allow_prompt=True):
    return Groq(api_key=get_api_key(allow_prompt))


def transcribe_audio(client, file_bytes, filename, model=DEFAULT_MODEL):
    """Transcribe raw audio bytes via Groq Whisper (English only).

    Returns (text, elapsed_seconds).
    """
    start = time.time()
    response = client.audio.transcriptions.create(
        file=(filename, file_bytes),
        model=model,
        language="en",
    )
    elapsed = time.time() - start
    return response.text, elapsed


_ALSA_LIB = ctypes.CDLL("libasound.so.2")


def raise_on_alsa_error(close_fn, handle, code, func_name):
    if code < 0:
        snd_strerror = _ALSA_LIB.snd_strerror
        snd_strerror.restype = ctypes.c_char_p
        snd_strerror.argtypes = [ctypes.c_int]
        msg = snd_strerror(code).decode()
        if handle:
            close_fn(handle)
        raise RuntimeError(f"ALSA {func_name} failed: {msg} (code {code})")


def record_mic(duration):
    """Record from the default ALSA capture device using ctypes. Returns numpy array."""
    lib = _ALSA_LIB
    snd_pcm_open = lib.snd_pcm_open
    snd_pcm_open.argtypes = [
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p,
        ctypes.c_int, ctypes.c_int,
    ]
    snd_pcm_open.restype = ctypes.c_int
    snd_pcm_close = lib.snd_pcm_close
    snd_pcm_close.argtypes = [ctypes.c_void_p]
    snd_pcm_close.restype = ctypes.c_int
    snd_pcm_hw_params_malloc = lib.snd_pcm_hw_params_malloc
    snd_pcm_hw_params_malloc.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    snd_pcm_hw_params_any = lib.snd_pcm_hw_params_any
    snd_pcm_hw_params_any.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    snd_pcm_hw_params_set_access = lib.snd_pcm_hw_params_set_access
    snd_pcm_hw_params_set_access.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
    ]
    snd_pcm_hw_params_set_format = lib.snd_pcm_hw_params_set_format
    snd_pcm_hw_params_set_format.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
    ]
    snd_pcm_hw_params_set_channels = lib.snd_pcm_hw_params_set_channels
    snd_pcm_hw_params_set_channels.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
    ]
    snd_pcm_hw_params_set_rate_near = lib.snd_pcm_hw_params_set_rate_near
    snd_pcm_hw_params_set_rate_near.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint), ctypes.c_int,
    ]
    snd_pcm_hw_params = lib.snd_pcm_hw_params
    snd_pcm_hw_params.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    snd_pcm_hw_params_free = lib.snd_pcm_hw_params_free
    snd_pcm_hw_params_free.argtypes = [ctypes.c_void_p]
    snd_pcm_readi = lib.snd_pcm_readi
    snd_pcm_readi.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
    ]
    snd_pcm_readi.restype = ctypes.c_long
    snd_pcm_prepare = lib.snd_pcm_prepare
    snd_pcm_prepare.argtypes = [ctypes.c_void_p]
    snd_pcm_prepare.restype = ctypes.c_int

    SND_PCM_STREAM_CAPTURE = 0
    SND_PCM_ACCESS_RW_INTERLEAVED = 3
    SND_PCM_FORMAT_S16_LE = 2

    handle = ctypes.c_void_p()
    params = ctypes.c_void_p()
    rate = ctypes.c_uint(SAMPLE_RATE)

    code = snd_pcm_open(ctypes.byref(handle), b"default",
                        SND_PCM_STREAM_CAPTURE, 0)
    raise_on_alsa_error(snd_pcm_close, handle, code, "snd_pcm_open")

    try:
        snd_pcm_hw_params_malloc(ctypes.byref(params))
        code = snd_pcm_hw_params_any(handle, params)
        raise_on_alsa_error(snd_pcm_close, handle, code, "snd_pcm_hw_params_any")
        code = snd_pcm_hw_params_set_access(
            handle, params, SND_PCM_ACCESS_RW_INTERLEAVED)
        raise_on_alsa_error(snd_pcm_close, handle, code, "snd_pcm_hw_params_set_access")
        code = snd_pcm_hw_params_set_format(
            handle, params, SND_PCM_FORMAT_S16_LE)
        raise_on_alsa_error(snd_pcm_close, handle, code, "snd_pcm_hw_params_set_format")
        code = snd_pcm_hw_params_set_channels(
            handle, params, ctypes.c_uint(CHANNELS))
        raise_on_alsa_error(snd_pcm_close, handle, code, "snd_pcm_hw_params_set_channels")
        code = snd_pcm_hw_params_set_rate_near(
            handle, params, ctypes.byref(rate), 0)
        raise_on_alsa_error(snd_pcm_close, handle, code, "snd_pcm_hw_params_set_rate_near")
        code = snd_pcm_hw_params(handle, params)
        raise_on_alsa_error(snd_pcm_close, handle, code, "snd_pcm_hw_params")
        snd_pcm_hw_params_free(params)

        period_size = SAMPLE_RATE // 10
        total_frames = int(SAMPLE_RATE * duration)
        frames = np.empty(total_frames, dtype=np.int16)
        buf = (ctypes.c_int16 * period_size)()
        offset = 0
        while offset < total_frames:
            n = min(period_size, total_frames - offset)
            code = snd_pcm_readi(handle, buf, ctypes.c_ulong(n))
            if code < 0:
                snd_pcm_prepare(handle)
                continue
            frames[offset:offset + code] = np.ctypeslib.as_array(buf)[:code]
            offset += code

        return frames.astype(np.float32) / 32768.0
    finally:
        snd_pcm_close(handle)


def transcribe(client, audio_path, model):
    with open(audio_path, "rb") as f:
        return transcribe_audio(client, f.read(), os.path.basename(audio_path), model)


def main():
    parser = argparse.ArgumentParser(
        description="Transcribe audio (mic or file) to English text via Groq Whisper.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--file", "-f", metavar="PATH",
        help="Transcribe an existing audio file (wav, mp3, flac, ogg, webm, ...).")
    source.add_argument(
        "--duration", "-d", type=float, default=10.0,
        help="Seconds to record from the microphone (default: 10).")
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"Whisper model (default: {DEFAULT_MODEL}).")
    parser.add_argument(
        "--out", "-o", metavar="PATH",
        help="Save a transcription to a text file instead of only printing.")
    args = parser.parse_args()

    client = get_client()

    audio_path = None
    tmp = None
    try:
        if args.file:
            audio_path = args.file
            if not os.path.isfile(audio_path):
                sys.exit(f"File not found: {audio_path}")
        else:
            if args.duration <= 0:
                sys.exit("--duration must be positive.")
            print(f"Recording for {args.duration:.0f}s from microphone... "
                  f"start speaking now.")
            try:
                samples = record_mic(args.duration)
            except RuntimeError as e:
                sys.exit(
                    f"Could not record from microphone.\n{e}\n"
                    "Tip: use --file to transcribe an existing audio file.")
            tmp = "recording.wav"
            sf.write(tmp, samples, SAMPLE_RATE)
            audio_path = tmp
            print(f"Recorded {args.duration:.0f}s ({len(samples)/SAMPLE_RATE:.1f}s "
                  f"of audio). Transcribing...")

        text, elapsed = transcribe(client, audio_path, args.model)

        print("\n" + "=" * 50)
        print(f"Transcription ({elapsed:.1f}s):")
        print("=" * 50)
        print(text)
        print("=" * 50)

        if args.out:
            with open(args.out, "w") as f:
                f.write(text + "\n")
            print(f"\nSaved to: {args.out}")
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


if __name__ == "__main__":
    main()
