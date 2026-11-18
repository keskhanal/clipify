# Contributing

Thanks for helping. This is a small project, so the process is light.

## Setup

```bash
git clone https://github.com/keskhanal/clipify.git
cd clipify
./skills/stream-clipper/setup.sh
```

## Trying a change

Test on a short slice so a run takes a couple of minutes instead of an hour:

```bash
PY=skills/stream-clipper/.venv/bin/python
CLIP=skills/stream-clipper/clipper.py

$PY $CLIP prep "<url>" --start 3600 --end 4320 --no-chat
# write .clipper/<id>/picks.json with one pick
$PY $CLIP render .clipper/<id>
```

Look at the `_preview.jpg` and `.clipper/<id>/clip01/facecam_check.jpg` before opening a pull request.

Run the offline tests too:

```bash
skills/stream-clipper/.venv/bin/python -m unittest discover -s tests -v
```

## Guidelines

- Keep `clipper.py` a single file with no dependencies beyond the ones in `setup.sh`.
- The picking rules live in `SKILL.md`. If you change what a good clip is, explain why in the pull request.
- Never commit downloaded audio, video, transcripts, or rendered clips. `.gitignore` covers `.clipper/` and `clips/`.
- For facecam detection bugs, attach the `facecam_check.jpg` (or describe the layout) so it can be reproduced.

## Good first issues

- Chat-spike detection for Twitch and Kick
- Finding streams by game on Twitch and Kick (needs their official APIs)
- Testing more sites (Rumble, Facebook Gaming, SOOP)
- Windows support
- Caption styles and layout options
