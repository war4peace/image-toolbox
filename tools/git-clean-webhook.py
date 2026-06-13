#!/usr/bin/env python
"""
git "clean" filter — blank discord_webhook_url before config.json is staged.

A clean filter runs whenever git reads the working tree into the index (e.g.
`git add config.json`): the file's content arrives on stdin and whatever this
script writes to stdout is what git actually stages. We blank the Discord
webhook so a live URL can never be committed by accident, while your working
copy keeps the real value.

Activated by two pieces:
  * .gitattributes:   config.json filter=tbx-webhook
  * a local git config (per machine, not committed):
        git config filter.tbx-webhook.clean "python tools/git-clean-webhook.py"

On a fresh clone the .gitattributes line names an undefined filter, which git
simply ignores (no error) — so set the git config line on each machine you
commit from. The script never blocks a commit: anything it can't parse is
passed through unchanged.
"""
import sys
import json

data = sys.stdin.read()
try:
    cfg = json.loads(data)
    ups = cfg.get("upscale")
    if isinstance(ups, dict) and ups.get("discord_webhook_url"):
        ups["discord_webhook_url"] = ""
        # Match save_config()'s formatting so an otherwise-unchanged file does
        # not show up as modified after the webhook is blanked.
        out = json.dumps(cfg, indent=4)
    else:
        out = data            # no webhook set — stage the bytes unchanged
except Exception:
    out = data                # not valid JSON — never block the commit
sys.stdout.write(out)
