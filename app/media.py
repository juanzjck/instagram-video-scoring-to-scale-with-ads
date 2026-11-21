"""Step 2 - preprocess the media: keyframes, timestamped transcript.

On-screen text is read by the vision model in step 3 (same call as the visual analysis), so
there is no separate OCR dependency here.

When a video has no clip on disk the dataset's keyframes.csv / transcripts.csv are used as
fixtures, and the result says so (`source: "fixture"`).
"""

import math
import re
import subprocess
import threading
import wave

import imageio_ffmpeg
import numpy as np

from . import config, ingest
from .config import FRAME_HEIGHT, WHISPER_MODEL

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
_whisper = None
_whisper_lock = threading.Lock()


def _ffmpeg(*args):
    return subprocess.run([FFMPEG, "-hide_banner", "-y", *args], capture_output=True, text=True)


def probe(clip):
    """(duration_s, has_audio) read from ffmpeg's stream report."""
    err = _ffmpeg("-i", str(clip)).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    dur = None if not m else int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return dur, bool(re.search(r"Stream #\d+:\d+.*Audio:", err))


def keyframe_times(duration_s):
    """Every second for the first 3 seconds, then every 3 seconds."""
    last = max(0, math.ceil(duration_s) - 1)
    times = [t for t in (0, 1, 2, 3) if t <= last]
    times += list(range(6, last + 1, 3))
    return times


def extract_keyframes(clip, out_dir, duration_s):
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i, t in enumerate(keyframe_times(duration_s)):
        name = f"kf_{i:02d}_t{t:03d}.jpg"
        # +0.2s lands inside the second rather than on a cut boundary
        seek = min(t + 0.2, max(0.0, duration_s - 0.05))
        r = _ffmpeg("-ss", f"{seek:.2f}", "-i", str(clip), "-frames:v", "1",
                    "-vf", f"scale=-2:{FRAME_HEIGHT}", "-q:v", "4", str(out_dir / name))
        if r.returncode == 0 and (out_dir / name).exists():
            frames.append({"frame_id": f"kf{i:02d}", "t_s": t, "file": name})
    return frames


def _model():
    global _whisper
    with _whisper_lock:
        if _whisper is None:
            from faster_whisper import WhisperModel
            _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        return _whisper


MIN_LANGUAGE_PROB = 0.5     # below this, Whisper's language guess is not trusted


def transcribe(clip, work_dir, hint=None):
    """Timestamped transcript via local Whisper: (segments, language code).

    The language is detected from the audio, so English and Spanish videos (or any other
    language Whisper knows) need no configuration. `hint` is the dataset's spoken_language;
    it is only used when detection is unsure, because a hint that is wrong for the video
    (a URL swapped under an existing row) would force a garbage transcript.
    ([], None) means the clip has no speech.
    """
    wav = work_dir / "audio.wav"
    r = _ffmpeg("-i", str(clip), "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", str(wav))
    if r.returncode != 0 or not wav.exists():
        return [], None
    # Pass samples, not the path: ffmpeg already decoded to 16 kHz mono PCM, and this keeps
    # faster-whisper's own (PyAV-based) decoder out of the picture.
    with wave.open(str(wav), "rb") as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    opts = dict(vad_filter=True, condition_on_previous_text=False)
    segments, info = _model().transcribe(audio, **opts)     # lazy: nothing decoded yet
    language = info.language
    if hint and hint != language and info.language_probability < MIN_LANGUAGE_PROB:
        segments, info = _model().transcribe(audio, language=hint, **opts)
        language = hint
    out = []
    for s in segments:
        text = s.text.strip()
        # Whisper invents words over music; drop segments it is itself unsure contain speech.
        if not text or s.no_speech_prob > 0.6 or s.avg_logprob < -1.0:
            continue
        out.append({"start_s": round(s.start, 2), "end_s": round(s.end, 2), "text": text})
    return out, (language if out else None)


def preprocess(video_id, row):
    """Keyframes + transcript for one video. Returns a JSON-serialisable dict."""
    work = config.RUNS / video_id
    work.mkdir(parents=True, exist_ok=True)
    clip = ingest.clip_path(video_id)

    if clip is None:
        frames = [dict(frame_id=f"kf{i:02d}", file=None, **f)
                  for i, f in enumerate(ingest.fixture_keyframes(video_id))]
        has_fixture = bool(frames)
        return {
            "video_id": video_id,
            "source": "fixture (no clip on disk; dataset keyframes.csv / transcripts.csv)"
                      if has_fixture else None,
            "duration_s": row.get("duration_s"),
            "frames": frames,
            "transcript": ingest.fixture_transcript(video_id) if has_fixture else None,
            "language": row.get("spoken_language") if has_fixture else None,
        }

    duration, has_audio = probe(clip)
    duration = duration or float(row.get("duration_s") or 0)
    frames = extract_keyframes(clip, work / "frames", duration)
    transcript, language = transcribe(clip, work, row.get("spoken_language")) if has_audio else ([], None)
    return {
        "video_id": video_id,
        "source": "extracted from clip (ffmpeg keyframes, Whisper transcript)",
        "duration_s": round(duration, 2),
        "frames": frames,
        "transcript": transcript,
        "language": language,
    }
