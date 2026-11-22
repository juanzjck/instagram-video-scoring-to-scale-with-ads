---
description: Start the PartGram web app in the background (auto-reload on code changes)
argument-hint: [port]
allowed-tools: Bash(scripts/start.sh:*), Bash(lsof:*)
---

Start the PartGram web app by running `scripts/start.sh $ARGUMENTS` from the repo root (default port 8000).

If it reports "already running", say so and give the URL; do not restart it.
If it fails, show the tail of `logs/server.log` and stop.
On success, reply with the URL and remind that Python edits under `app/` reload automatically
(`--reload`), while the browser still needs a refresh.
