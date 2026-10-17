"""Fetch a video from the URL the reviewer provides.

A direct link to a media file is streamed to disk. A link to a page (Instagram, TikTok,
YouTube, ...) is handed to yt-dlp. Either way the result must be something ffmpeg can read.
"""

import shutil
from urllib.parse import urlparse

import httpx

from . import media

MAX_BYTES = 500 * 1024 * 1024
_NOT_MEDIA = ("text/", "application/json", "application/xhtml+xml")


def check_url(url):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError(f"not an http(s) URL: {url!r}")
    return url


def _direct(url, out):
    """Stream the URL to `out`. False when the response is a page, not a media file."""
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(30, read=120),
                      headers={"User-Agent": "Mozilla/5.0"}) as r:
        r.raise_for_status()
        if r.headers.get("content-type", "").lower().startswith(_NOT_MEDIA):
            return False
        size = 0
        with out.open("wb") as f:
            for chunk in r.iter_bytes(1 << 16):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError(f"video is larger than {MAX_BYTES // 2**20} MB")
                f.write(chunk)
    return True


def _via_ytdlp(url, out):
    try:
        import yt_dlp
    except ImportError as e:
        raise RuntimeError("this URL is a web page, not a video file; "
                           "install yt-dlp to fetch from video sites") from e
    opts = {"format": "bv*[height<=1080]+ba/b", "merge_output_format": "mp4",
            "outtmpl": str(out.with_suffix("")) + ".%(ext)s", "ffmpeg_location": media.FFMPEG,
            "noplaylist": True, "max_filesize": MAX_BYTES, "overwrites": True,
            "quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        got = (info.get("requested_downloads") or [{}])[0].get("filepath") or ydl.prepare_filename(info)
    if str(got) != str(out):
        shutil.move(got, out)


def download(url, dest):
    """Download `url` to `dest`. Raises with a readable reason when it is not a usable video."""
    check_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name("download_" + dest.name)
    try:
        try:
            direct = _direct(url, tmp)
        except httpx.HTTPError:
            direct = False               # some sites refuse a plain GET; yt-dlp may still work
        if not direct:
            _via_ytdlp(url, tmp)
        duration, _ = media.probe(tmp)
        if not duration:
            raise ValueError("the URL did not return a video ffmpeg can read")
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest
