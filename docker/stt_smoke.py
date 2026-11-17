"""Build-time proof that the image can decode the audio it will be sent.

Importing faster_whisper proves nothing about transcription: images built
with av 19 imported it cleanly and then failed on every file, because
``faster_whisper.audio.decode_audio`` passes ``av.open()`` a keyword av 19
removed. This script makes the image build run that same call, on the
containers the channels actually deliver: Ogg/Opus (Telegram voice, Firefox
recordings), WebM/Opus (Chrome recordings), MP4/AAC (Safari and iOS) and WAV
(the CLI recorder).

It stops short of running the model. The weights are not in the image (they
download at runtime to the model cache), and fetching them during the build
would make every build depend on the network. What it does exercise is the
step that broke, end to end: encode a known tone with PyAV, hand the bytes to
the decoder faster-whisper itself uses, and check that the samples that come
back are the length and loudness of the tone that went in.

Run as ``python docker/stt_smoke.py``. Exit status 0 means every container
decoded; anything else fails the build.
"""

from __future__ import annotations

import io
import math
import sys
import wave

SAMPLE_RATE = 16000
SECONDS = 1.0
FREQ_HZ = 440.0


def _tone() -> list[float]:
    n = int(SAMPLE_RATE * SECONDS)
    return [0.5 * math.sin(2 * math.pi * FREQ_HZ * i / SAMPLE_RATE) for i in range(n)]


def _wav_bytes(samples: list[float]) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(b"".join(int(s * 32767).to_bytes(2, "little", signed=True) for s in samples))
    return buf.getvalue()


def _encoded_bytes(samples: list[float], fmt: str, codec: str, rate: int) -> bytes:
    import av
    import numpy as np

    buf = io.BytesIO()
    with av.open(buf, mode="w", format=fmt) as out:
        stream = out.add_stream(codec, rate=rate)
        stream.layout = "mono"
        pcm = np.array([samples], dtype=np.float32)
        frame = av.AudioFrame.from_ndarray(pcm, format="fltp", layout="mono")
        frame.sample_rate = SAMPLE_RATE
        resampler = av.AudioResampler(format=stream.format, layout="mono", rate=rate)
        frame_size = stream.codec_context.frame_size or 1024
        fifo = av.AudioFifo()
        for f in resampler.resample(frame):
            fifo.write(f)
        for f in resampler.resample(None):
            fifo.write(f)
        while fifo.samples >= frame_size:
            for packet in stream.encode(fifo.read(frame_size)):
                out.mux(packet)
        if fifo.samples:
            for packet in stream.encode(fifo.read()):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    return buf.getvalue()


def main() -> int:
    import av
    from faster_whisper.audio import decode_audio

    samples = _tone()
    cases = {
        "wav": _wav_bytes(samples),
        "ogg/opus": _encoded_bytes(samples, "ogg", "libopus", 48000),
        "webm/opus": _encoded_bytes(samples, "webm", "libopus", 48000),
        "mp4/aac": _encoded_bytes(samples, "mp4", "aac", SAMPLE_RATE),
    }
    failures: list[str] = []
    for name, data in cases.items():
        try:
            decoded = decode_audio(io.BytesIO(data), sampling_rate=SAMPLE_RATE)
        except Exception as exc:  # report every container, not the first
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        seconds = len(decoded) / SAMPLE_RATE
        peak = float(abs(decoded).max()) if len(decoded) else 0.0
        # Codecs pad and trim a few milliseconds and resample the amplitude
        # slightly; a decoder that returns silence or a stub fails these.
        if not 0.8 <= seconds <= 1.2 or peak < 0.2:
            failures.append(f"{name}: decoded {seconds:.3f}s with peak {peak:.3f}")
        else:
            print(f"stt smoke {name}: {seconds:.3f}s peak {peak:.3f}")
    if failures:
        print(f"stt smoke FAILED with av {av.__version__}:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"stt smoke ok (av {av.__version__})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
