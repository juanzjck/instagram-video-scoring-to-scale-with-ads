---
description: Stop the PartGram web app (force-kills whatever listens on port 8000)
---
Stop the PartGram web app by running `scripts/stop.sh` from the repo root. The script kills the
pid-file process and force-kills anything still listening on the port (default 8000), so no
confirmation is needed. Report which PIDs were killed, or that nothing was running.
