"""Uploaded datasets: save the CSVs under uploads/<id>/, validate them, make them active."""

import re
import shutil
from datetime import datetime
from pathlib import PurePath

import pandas as pd

from . import config, ingest

REQUIRED = {
    "videos.csv": ["video_id", "creator_id", "campaign_id"],
    "creators.csv": ["creator_id"],
    "campaigns.csv": ["campaign_id"],
}
OPTIONAL = ["comments.csv", "media.csv", "keyframes.csv", "transcripts.csv",
            "ground_truth.csv", "ground_truth_media.csv"]
KNOWN = [*REQUIRED, *OPTIONAL]
MAX_FILE_BYTES = 20 * 1024 * 1024
_SAFE_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")     # video ids become folder names


def _validate(folder):
    for name, cols in REQUIRED.items():
        if not (folder / name).exists():
            raise ValueError(f"{name} is required (also needed: {', '.join(REQUIRED)})")
        df = pd.read_csv(folder / name, dtype=str)
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise ValueError(f"{name} is missing column(s): {', '.join(missing)}")
        if name == "videos.csv":
            ids = df["video_id"].dropna().tolist()
            if not ids or len(ids) != len(df):
                raise ValueError("videos.csv has no rows, or rows with a blank video_id")
            if len(set(ids)) != len(ids):
                raise ValueError("videos.csv has duplicate video_id values")
            bad = [i for i in ids if not _SAFE_ID.match(i)]
            if bad:
                raise ValueError(f"video_id may only use letters, digits, _ . - (got {bad[0]!r})")


def save(files):
    """files: [(filename, bytes)]. Stores the recognised CSVs as a new dataset and activates it.

    Raises ValueError (and leaves the previous dataset active) when the upload is not usable.
    """
    picked, ignored = {}, []
    for filename, data in files:
        name = PurePath(filename or "").name.lower()
        if name in KNOWN:
            picked[name] = data
        else:
            ignored.append(filename)
    if not picked:
        raise ValueError(f"no recognised files; expected any of: {', '.join(KNOWN)}")

    config.UPLOADS.mkdir(exist_ok=True)
    dataset_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = config.UPLOADS / dataset_id
    folder.mkdir()
    previous = config.DATASET_ID
    try:
        for name, data in picked.items():
            (folder / name).write_bytes(data)
        _validate(folder)
        config.activate(dataset_id)
        ingest.load_inputs.cache_clear()
        ingest.load_inputs()                 # the joins themselves can still reject the data
    except Exception as e:
        config.activate(previous)
        ingest.load_inputs.cache_clear()
        shutil.rmtree(folder, ignore_errors=True)
        raise ValueError(str(e)) from e
    return {**describe(), "ignored": ignored}


def use(dataset_id):
    """Switch to an earlier upload, or to the example dataset when dataset_id is None."""
    config.activate(dataset_id)
    ingest.load_inputs.cache_clear()
    return describe()


def describe():
    uploads = []
    if config.UPLOADS.is_dir():
        uploads = sorted((p.name for p in config.UPLOADS.iterdir() if p.is_dir()), reverse=True)
    return {
        "id": config.DATASET_ID,
        "source": "upload" if config.DATASET_ID else "example",
        "path": str(config.DATA),
        "files": [n for n in KNOWN if (config.DATA / n).exists()],
        "videos": len(ingest.load_inputs()[0]),
        "uploads": uploads,
    }
