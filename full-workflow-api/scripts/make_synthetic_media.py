"""Build a synthetic clip.mp4 for every video in the dataset, so the real pipeline
(ffmpeg keyframes -> Whisper -> Claude vision) has pixels and audio to work on.

Source of truth is the dataset itself:
  keyframes.csv   -> one rendered still per row, held until the next row's timestamp.
                     The scene description is drawn as text; the on-screen text is drawn
                     as a burned-in caption (including vid_003's injection string).
  transcripts.csv -> speech via macOS `say`, in the voice for each segment's language
                     (Spanish or English), placed at each segment's start time.
  media.csv       -> duration and audio type (videos with no voice get a quiet tone bed).

These are stand-ins, not footage: the vision model reads a written scene description
instead of seeing the scene. Drop a real clip.mp4 into data-example/media/<video_id>/
and the pipeline uses it unchanged.

With --ai-images each still is generated from its scene description, so the vision step
looks at a picture instead of reading a sentence:
  --ai-images            Claude draws the scene as an SVG illustration (default; no extra key)
  --ai-images=openai     a raster image model paints it (needs OPENAI_API_KEY)
The on-screen text is still drawn in code, on top of the generated picture, because the
answer key depends on those strings being exact. Generated pictures are cached in
data-example/media/<video_id>/ai/<provider>/ and reused on the next run.

Usage:  python scripts/make_synthetic_media.py [--force] [--ai-images[=claude|openai]]
                                               [--regen-images] [video_id ...]
"""

import colorsys
from concurrent.futures import ThreadPoolExecutor
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import imageio_ffmpeg
import pandas as pd
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import genmedia  # noqa: E402
from app.config import DATA, MEDIA  # noqa: E402

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
W, H = 540, 960
FONT = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"
# transcripts.csv language -> `say` voice; falls back to the system default voice if missing
VOICES = genmedia.SAY_VOICES                # the same voices the campaign recap is narrated in


def font(size):
    try:
        return ImageFont.truetype(FONT, size)
    except OSError:
        return ImageFont.load_default(size)


def clean(text):
    """Drop emoji the font cannot draw."""
    return re.sub(r"[\U00010000-\U0010FFFF☀-➿️]", "", str(text)).strip()


IMAGE_PROMPT = """A single frame from a vertical smartphone video posted as an Instagram Reel by a \
creator. Photorealistic, handheld phone camera, natural light, casual and unstaged.

Scene: {scene}

Context: the video belongs to a campaign for the brand "{brand}" ({category}). Wherever the \
scene mentions that brand's product, packaging or logo, show the brand name "{brand}" clearly \
on it. If the scene does not mention the product, do not add one.

Do not add captions, subtitles, stickers, watermarks, app interface elements or any overlaid \
text. The only text allowed is text physically printed on objects in the scene."""


SVG_PROMPT = """Scene to draw: {scene}

It is one frame of a creator's vertical video for the brand "{brand}" ({category}). Wherever \
the scene mentions that brand's product, packaging, logo or app, write "{brand}" on it, large \
enough to read. If the scene does not mention the product, do not draw one.

Keep the important parts of the picture in the upper two thirds: a caption bar is laid over \
the band between y=620 and y=780 afterwards. That bar carries every overlay the scene \
mentions (captions, discount codes, links, hashtags), so draw none of them yourself, and do \
not invent offers, prices, codes or slogans: no banners, badges or stickers."""


def ai_image(video_id, idx, t_s, scene, ai):
    """The generated picture for one keyframe, cropped to the frame. Cached on disk."""
    cache = MEDIA / video_id / "ai" / ai["provider"] / f"kf_{idx:02d}_t{int(t_s):03d}.png"
    if ai["regen"] or not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        fields = dict(scene=clean(scene), brand=ai["brand"], category=ai["category"])
        if ai["provider"] == "claude":
            genmedia.claude_image(SVG_PROMPT.format(**fields), cache, W, H)
        else:
            genmedia.image(IMAGE_PROMPT.format(**fields), cache)
        print(f"  generated {cache.relative_to(MEDIA)}", flush=True)
    img = Image.open(cache).convert("RGB")
    scale = max(W / img.width, H / img.height)
    img = img.resize((round(img.width * scale), round(img.height * scale)))
    left, top = (img.width - W) // 2, (img.height - H) // 2
    return img.crop((left, top, left + W, top + H))


def render_frame(path, video_id, idx, t_s, scene, overlay, picture=None):
    if picture is not None:
        img = picture
        d = ImageDraw.Draw(img)
    else:
        hue = (int(video_id[-3:]) * 0.137) % 1.0
        top = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.45, 0.55 - 0.03 * (idx % 4)))
        img = Image.new("RGB", (W, H), top)
        d = ImageDraw.Draw(img)

        d.text((20, 18), f"SYNTHETIC MOCK FRAME  {video_id}  t={t_s}s", font=font(16), fill=(255, 255, 255))
        d.text((20, 300), "SCENE", font=font(18), fill=(230, 230, 230))
        y = 330
        for line in textwrap.wrap(clean(scene), 32):
            d.text((20, y), line, font=font(26), fill=(255, 255, 255))
            y += 36

    if overlay:
        lines = textwrap.wrap(clean(overlay), 24)
        box_h = 28 + 44 * len(lines)
        d.rectangle([0, H - 190 - box_h, W, H - 190], fill=(0, 0, 0))
        y = H - 190 - box_h + 14
        for line in lines:
            d.text((W / 2, y), line, font=font(32), fill=(255, 255, 255), anchor="ma")
            y += 44
    img.save(path)


def ff(*args):
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-600:])


def build(video_id, duration, frames, segments, tmp, ai=None):
    """ai: None for drawn slides, or {"provider", "brand", "category", "regen"} to generate
    the pictures."""
    out_dir = MEDIA / video_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- video track: stills held between keyframe timestamps
    pictures = [None] * len(frames)
    if ai:                                     # one model call per still; run them side by side
        with ThreadPoolExecutor(max_workers=4) as pool:
            pictures = list(pool.map(
                lambda x: ai_image(video_id, x[0], x[1]["t_s"], x[1]["scene_description"], ai),
                enumerate(frames)))
    concat = []
    for i, f in enumerate(frames):
        png = tmp / f"{video_id}_{i:02d}.png"
        render_frame(png, video_id, i, f["t_s"], f["scene_description"], f["on_screen_text"], pictures[i])
        end = frames[i + 1]["t_s"] if i + 1 < len(frames) else duration
        concat += [f"file '{png}'", f"duration {max(0.04, end - f['t_s'])}"]
    concat.append(f"file '{png}'")                 # concat demuxer needs the last file repeated
    listing = tmp / f"{video_id}.txt"
    listing.write_text("\n".join(concat), encoding="utf-8")
    shutil.copy(tmp / f"{video_id}_00.png", out_dir / "thumb.png")

    # ---- audio track: tone bed + one speech clip per transcript segment
    inputs = ["-f", "lavfi", "-t", str(duration), "-i", "sine=frequency=196:sample_rate=44100"]
    filters, labels = ["[1:a]volume=0.02[bed]"], ["[bed]"]
    for j, s in enumerate(segments):
        aiff = tmp / f"{video_id}_seg{j}.aiff"
        voice = VOICES.get(str(s.get("language")), VOICES["es"])
        cmd = ["say", "-v", voice, "-r", str(genmedia.SAY_RATE), "-o", str(aiff), clean(s["text"])]
        if subprocess.run(cmd, capture_output=True).returncode != 0:
            subprocess.run(["say", "-o", str(aiff), clean(s["text"])], check=True)
        inputs += ["-i", str(aiff)]
        ms = int(float(s["start_s"]) * 1000)
        filters.append(f"[{j + 2}:a]aresample=44100,adelay={ms}:all=1[s{j}]")
        labels.append(f"[s{j}]")
    filters.append(f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=longest[a]")

    ff("-f", "concat", "-safe", "0", "-i", str(listing), *inputs,
       "-filter_complex", ";".join(filters), "-map", "0:v", "-map", "[a]",
       "-vf", "fps=25,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
       "-c:a", "aac", "-b:a", "96k", "-t", str(duration), "-movflags", "+faststart",
       str(out_dir / "clip.mp4"))


def main():
    flags = dict(a.partition("=")[::2] for a in sys.argv[1:] if a.startswith("--"))
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    force, regen, use_ai = "--force" in flags, "--regen-images" in flags, "--ai-images" in flags
    provider = flags.get("--ai-images") or "claude"
    if use_ai and provider not in ("claude", "openai"):
        sys.exit("--ai-images takes claude or openai")
    if use_ai and provider == "openai" and not genmedia.available():
        sys.exit("--ai-images=openai needs OPENAI_API_KEY in full-workflow-api/.env")
    brands = (pd.read_csv(DATA / "videos.csv").merge(pd.read_csv(DATA / "campaigns.csv"), on="campaign_id")
              .set_index("video_id")[["brand", "brand_category"]])
    media = pd.read_csv(DATA / "media.csv")
    kf = pd.read_csv(DATA / "keyframes.csv").fillna("")
    tr = pd.read_csv(DATA / "transcripts.csv")

    with tempfile.TemporaryDirectory() as tmp:
        for _, m in media.iterrows():
            vid = m["video_id"]
            if only and vid not in only:
                continue
            target = MEDIA / vid / "clip.mp4"
            if target.exists() and not force:
                print(f"{vid}: exists, skipped (use --force to rebuild)")
                continue
            frames = kf[kf["video_id"] == vid].sort_values("t_s").to_dict("records")
            segs = tr[tr["video_id"] == vid].sort_values("start_s").to_dict("records")
            if not frames:
                print(f"{vid}: no keyframes in dataset, skipped")
                continue
            ai = {"provider": provider, "brand": brands.loc[vid, "brand"],
                  "category": brands.loc[vid, "brand_category"], "regen": regen} if use_ai else None
            build(vid, float(m["duration_s"]), frames, segs, Path(tmp), ai)
            print(f"{vid}: {target.stat().st_size / 1024:.0f} KB, {len(frames)} "
                  f"{provider + '-generated' if use_ai else 'drawn'} stills, {len(segs)} speech segments")


if __name__ == "__main__":
    main()
