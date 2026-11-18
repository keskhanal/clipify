# Clipify

A [Claude Code](https://claude.com/claude-code) skill that turns long stream VODs into ready-to-post vertical clips.

Give it a stream link from YouTube, Twitch, Kick or most other streaming sites (or let it find streams for you) and you get 1080x1920 clips with the facecam on top, gameplay below, a hook title, word-by-word captions, and a post caption with hashtags.

## How it works

1. **Audio only.** It downloads just the audio of the stream, so a 4-hour VOD is a couple hundred MB instead of a giant video file.
2. **Listens locally.** Whisper transcribes the stream on your machine with word timestamps. The script also marks where the streamer got loud and, on YouTube replays, where chat suddenly sped up.
3. **Claude picks.** Claude reads the transcript 12 minutes at a time, scores every candidate moment, and throws out anything that needs context or has no payoff.
4. **Renders.** It downloads only the seconds it needs, finds the facecam, and builds the clip with ffmpeg.

Everything except the picking runs on your computer. No API keys.

## Platforms

| | Clip from a link | Find streams | Chat-spike signal |
| --- | --- | --- | --- |
| YouTube | yes | by topic or by channel | yes, when the stream has a chat replay |
| Twitch | yes, including broadcasts still live | by channel | no |
| Kick | yes, including broadcasts still live | by channel | no |
| Other [yt-dlp sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md) | should work, untested | no | no |

Without a chat replay, moments are picked from the transcript and the loudness signal alone. A YouTube stream that is still live has to finish and publish its replay first.

## Requirements

- macOS or Linux (developed and tested on Apple Silicon)
- [Claude Code](https://claude.com/claude-code)
- Python 3.11+
- ffmpeg (`brew install ffmpeg`)
- Node.js (recommended, helps YouTube downloads)
- About 1.5 GB of disk for the Python environment and Whisper model

## Install

```bash
git clone https://github.com/keskhanal/clipify.git
cd clipify
./install.sh
```

`install.sh` links the skill into `~/.claude/skills/` so it is available in every Claude Code session, then builds its private Python environment.

## Use

Open Claude Code in the folder where you want your clips and say:

```
clip this stream https://www.youtube.com/watch?v=...
```

or

```
find today's biggest GTA 6 streams and clip them
```

or

```
clip xQc's latest Kick stream
```

Clips land in `./clips/<video id>/`:

| File | What it is |
| --- | --- |
| `01_<hook>.mp4` | the clip, 1080x1920 |
| `01_<hook>.txt` | hook, post caption, hashtags, source link with timestamp |
| `01_<hook>_preview.jpg` | one frame, for a quick look |

### Without Claude

The script also works by hand; you just do the picking yourself.

```bash
PY=skills/stream-clipper/.venv/bin/python
CLIP=skills/stream-clipper/clipper.py

$PY $CLIP find "GTA 6 stream"            # biggest recent YouTube streams
$PY $CLIP find --platform twitch --channel timthetatman   # a streamer's past broadcasts
$PY $CLIP find --platform kick --channel xqc
$PY $CLIP prep "<url>"                   # transcript windows in .clipper/<id>/windows/
# write .clipper/<id>/picks.json (format in skills/stream-clipper/SKILL.md)
$PY $CLIP render .clipper/<id> --top 3
```

Useful flags:

- `--cookies chrome` (before the subcommand) for age-restricted or login-only streams
- `prep --start S --end S` to process part of a stream
- `prep --model small|medium|large-v3-turbo` to choose the Whisper size (default picks by RAM)
- `render --facecam x,y,w,h` to set the facecam box by hand
- `render --layout nocam` for a plain centre crop

## Project layout

```
skills/stream-clipper/
  SKILL.md      instructions Claude follows, including the picking rubric
  clipper.py    find / prep / render
  setup.sh      builds the Python environment
tests/          offline tests
install.sh      links the skill into ~/.claude/skills and runs setup
```

## How long it takes

Measured on an 8 GB Apple Silicon Mac with the `small` Whisper model:

| Step | 46-minute YouTube stream |
| --- | --- |
| `prep` (audio, loudness, transcript) | 5 minutes |
| `render` 3 clips (40 to 70 seconds each) | under 3 minutes |

The chat replay is extra: a busy 3-hour chat took about 10 minutes to download. `prep --no-chat` skips it. `prep` is resumable, so rerunning it after an interruption continues where it stopped.

## Facecam detection

Each clip is checked separately, because layouts change during a stream.

- It looks for a face that moves, so faces in thumbnails, posters and web pages are ignored.
- It then looks for the camera's border and puts that box in the top panel (`cam`).
- If the camera is the main picture (wider than 40% of the frame), the clip is one 9:16 crop that follows the face (`fullcam`).
- If no moving face is found, the clip is a plain centre crop (`nocam`).

Every render writes a preview frame, and `.clipper/<id>/clipNN/facecam_check.jpg` shows the detected box. If it is wrong, pass `--facecam x,y,w,h`.

## Known limits

- Captions are Whisper output. Names and game titles are sometimes misheard, more so with the `small` model.
- The loudness tag is a rough hint; on streams that are loud throughout it marks a lot of lines.
- Words that fall on a 20-minute transcription boundary can be clipped.
- Twitch and Kick: no chat signal, and streams can only be found by streamer name, not by game.
- Linux uses `faster-whisper` instead of `mlx-whisper`; that path is written but untested. Windows is not supported.

## Tests

```bash
skills/stream-clipper/.venv/bin/python -m unittest discover -s tests -v
```

The tests run offline (no network, no Whisper) and cover pick validation, caption timing, facecam border detection, and the full ffmpeg render on a generated clip.

## Status

Early but working. Checked end to end on a full 46-minute YouTube stream and on slices of Twitch and Kick broadcasts, across corner, full-frame and portrait cameras. Multi-hour runs and other sites need more testing. Issues and pull requests are welcome, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Use it responsibly

Clips are other people's content. Clip streamers who allow clipping (many run clipping programs), credit them, follow each platform's rules, and watch every clip before you post it. You are responsible for what you publish.

## License

[MIT](LICENSE)
