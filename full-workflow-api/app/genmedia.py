"""Generated media: pictures and voice.

Pictures - two ways:
  claude_image()  Claude has no image output, but it writes code. It draws the scene as an SVG
                  illustration, which is rendered to PNG here. No extra key; the result is a
                  drawing, not a photograph. Text in it is exact, because it is real text.
  image()         A raster image model (OpenAI). Photographic, but needs OPENAI_API_KEY and
                  garbles text.
Voice:
  speak()         OpenAI neural text-to-speech when a key is set, macOS `say` otherwise.

Never generate anything that must be exact (charts, scores, captions). Those stay in Pillow.
"""

import base64
import re
import shutil
import subprocess
import wave
from functools import lru_cache

from . import config, llm
from .media import FFMPEG

# One voice per language, shared by the example clips (scripts/make_synthetic_media.py) and the
# campaign recap, so a recap sounds like the videos it is about.
SAY_VOICES = {"es": "Paulina", "en": "Samantha"}
SAY_RATE = 185              # words per minute
LANGUAGES = {"en": "English", "es": "Spanish"}


def available():
    return bool(config.OPENAI_API_KEY)


@lru_cache(maxsize=1)
def _client():
    from openai import OpenAI
    return OpenAI(api_key=config.OPENAI_API_KEY, max_retries=3)


def tts_engine():
    """Which voice speak() will use: 'openai', or 'say' as the offline fallback."""
    if config.TTS_PROVIDER in ("openai", "say"):
        return config.TTS_PROVIDER
    return "openai" if available() else "say"


def _to_wav(src, wav):
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                        "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg: " + r.stderr.strip()[-300:])
    with wave.open(str(wav), "rb") as w:
        return w.getnframes() / w.getframerate()


def speak(text, wav, language="en", style=None):
    """Read `text` aloud into a 44.1 kHz mono wav. Returns (seconds, voice description)."""
    if tts_engine() == "openai":
        if not available():
            raise RuntimeError("TTS_PROVIDER=openai but OPENAI_API_KEY is not set")
        raw = wav.with_suffix(".tts.wav")
        instructions = (f"Speak in {LANGUAGES.get(language, language)}. "
                        + (style or "Clear, warm and natural."))
        with _client().audio.speech.with_streaming_response.create(
                model=config.TTS_MODEL, voice=config.TTS_VOICE, input=text,
                instructions=instructions, response_format="wav") as resp:
            resp.stream_to_file(raw)
        return _to_wav(raw, wav), f"openai {config.TTS_MODEL} ({config.TTS_VOICE})"

    if not shutil.which("say"):
        raise RuntimeError("no text-to-speech available: set OPENAI_API_KEY, or run on macOS for `say`")
    aiff = wav.with_suffix(".aiff")
    voice = SAY_VOICES.get(language)
    base = ["say", "-r", str(SAY_RATE), "-o", str(aiff)]
    if not voice or subprocess.run([*base, "-v", voice, text], capture_output=True).returncode != 0:
        subprocess.run([*base, text], check=True, capture_output=True)
    return _to_wav(aiff, wav), f"macOS say ({voice or 'default'})"


def image(prompt, path, size="1024x1536"):
    """Generate one image from a prompt and save it as PNG."""
    if not available():
        raise RuntimeError("image generation needs OPENAI_API_KEY")
    resp = _client().images.generate(model=config.IMAGE_MODEL, prompt=prompt, size=size,
                                     quality=config.IMAGE_QUALITY, n=1)
    path.write_bytes(base64.b64decode(resp.data[0].b64_json))
    return path


_SVG_SYSTEM = """You are an illustrator who draws with SVG code. You are given a scene and you \
return one complete SVG document that depicts it, and nothing else: no explanation, no \
markdown fence.

Requirements:
- Root element: <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}">. Fill the whole \
frame; the first element is a full-size background.
- A detailed, polished flat illustration, like a frame from a vertical phone video: a real \
setting (walls, furniture, street, counter), people drawn with faces, hair, hands and \
clothing, objects with recognisable shapes, depth from overlapping layers, light and shadow \
from gradients. Draw what the scene says, as literally as you can: someone looking at the \
picture must be able to tell what is happening without being told.
- Text: none, except words that would be physically printed on an object in the scene (a \
product label, a logo, an app screen). Never write the scene description, a title, a \
caption or a timestamp into the picture.
- Only shapes, paths, gradients and <text> with font-family Arial. No <image>, <script>, \
<foreignObject>, <style> imports, animation or external references."""

_SVG_RE = re.compile(r"<svg\b.*?</svg>", re.DOTALL | re.IGNORECASE)
_SVG_BANNED = re.compile(r"<\s*(script|image|foreignObject|iframe|use\s[^>]*href\s*=\s*[\"']?http)", re.IGNORECASE)


def claude_image(prompt, path, width=540, height=960):
    """Have Claude draw `prompt` as an SVG and render it to a PNG at `path`.

    The SVG source is kept beside the PNG. Returns the token usage of the call.
    """
    import resvg_py

    resp = llm.complete(prompt, system=_SVG_SYSTEM.format(w=width, h=height), max_tokens=12000,
                        stream=True)             # a detailed drawing takes a minute or two to write
    m = _SVG_RE.search(resp["text"])
    if not m:
        why = "ran out of tokens" if resp["stop_reason"] == "max_tokens" else "no <svg> in the reply"
        raise RuntimeError(f"Claude did not return a complete SVG ({why})")
    svg = m.group(0)
    if _SVG_BANNED.search(svg):
        raise RuntimeError("the SVG uses an element that is not allowed (script, image or external reference)")
    path.with_suffix(".svg").write_text(svg, encoding="utf-8")
    path.write_bytes(resvg_py.svg_to_bytes(svg_string=svg, width=width, height=height, background="#ffffff"))
    return {k: resp[k] for k in ("input_tokens", "output_tokens", "latency_s", "model")}
