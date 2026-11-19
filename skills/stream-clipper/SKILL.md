---
name: stream-clipper
description: Turn a long stream VOD (YouTube, Twitch, Kick and other yt-dlp sites) into ready-to-post vertical clips - facecam on top, gameplay below, hook title, word-by-word captions, plus a post caption and hashtags. Use when the user says "clip this stream", pastes a stream link and asks for clips/shorts, asks to find today's biggest streams for a game and clip them, or says "set up the clipper".
---

# Stream clipper

Everything lives next to this file. Call the script as:

```
SKILL=<directory of this SKILL.md>
$SKILL/.venv/bin/python $SKILL/clipper.py <command>
```

If `$SKILL/.venv` does not exist (or the user says "set up the clipper"), run `$SKILL/setup.sh` first. It checks ffmpeg and builds a private Python env; tell the user when it is ready.

Run commands from the user's project folder. Work files go to `./.clipper/<video id>/`, finished clips to `./clips/<video id>/`.

## 1. Get a stream

- User gave a link: use it. Any past broadcast works: YouTube, Twitch (`twitch.tv/videos/...`), Kick (`kick.com/<name>/videos/...`), and most other sites yt-dlp supports (Rumble, Facebook, SOOP and so on).
- No link, a topic: `clipper.py find "<game or topic> stream"` (for example `"GTA 6 stream"`) searches YouTube for recent long videos by views. `--when week` widens the date range.
- No link, a streamer: `clipper.py find --platform twitch|kick|youtube --channel <name>` lists that streamer's past broadcasts, newest first. Add a word like `"gta"` to filter by title. Twitch and Kick cannot be searched by topic, only by streamer, so if the user names a game and wants Twitch or Kick, ask which streamers (or suggest the well-known ones for that game) and list each.
- A standing list of streamers: `clipper.py watch` reads `./watchlist.json` (`{"query": "gta", "streamers": {"twitch": [...], "kick": [...], "youtube": [...]}}`) and lists each streamer's newest matching broadcasts across all three platforms, biggest first. Ones already worked on are tagged `[CLIPPED]`, `[PREPPED]` or `[PREP UNFINISHED]` and listed last. Use it when the user wants "the latest" streams to clip; speed matters, since the first clip of a moment to be posted gets most of the views.
- Pick the biggest relevant ones, tell the user which, and continue.

Live streams: a YouTube stream that is still live cannot be clipped until its replay is published. A Twitch or Kick broadcast that is still live can be clipped up to the present moment. Entries marked `[LIVE NOW]` in `find` are still running.

## 2. Prep (long-running, run it in the background)

```
clipper.py prep "<url>"
```

This downloads only the audio, transcribes it locally with Whisper (word timestamps), measures where the streamer got loud, and on YouTube replays where chat sped up (other platforms do not provide a chat replay, so their windows only carry `{LOUD}` tags). It writes `windows/w001.txt, w002.txt, ...`, 12 minutes of transcript each. It is resumable: rerun the same command after an interruption.

- Speed on an 8 GB Apple Silicon Mac with the `small` model: a 46-minute stream took 5 minutes to prep. Expect roughly 25 to 30 minutes for a 4-hour stream. Transcribe one stream at a time on machines with 8 GB of memory; with 16 GB or more, several `prep` runs can go in parallel.
- Age-restricted, subscriber-only or login-only streams: add `--cookies chrome` before the subcommand, for example `clipper.py --cookies chrome prep "<url>"`.
- `--start S --end S` limits the run to part of the stream. `--no-chat` skips the chat replay. On a big stream it is the slowest step (about 10 minutes for a busy 3-hour chat), so skip it when speed matters more than the extra signal.

## 3. Pick the moments (your job)

Read every window file, one at a time. Do not skim and do not sample: a moment you never read cannot be picked. For streams with many windows, split them across subagents and have each return candidates in the format below.

Line format: `[start-end seconds | H:MM:SS] text {LOUD} {CHAT x3.1}`. The tags are hints, not verdicts: a loud line with nothing said is not a clip, and a quiet line can be the best one. Many streams have no chat replay at all (prep says so); then there are no `{CHAT}` tags and you pick from the words and `{LOUD}` alone.

What makes a clip people share:
- first reactions (seeing something for the first time)
- crazy discoveries, unexpected things in the game
- full freakouts, rage, panic
- funny fails and bits
- hot takes, strong opinions said plainly

Be harsh. Throw out anything that:
- needs earlier context to make sense
- starts mid-thought ("uh, so yeah...") with no clean entry a few seconds earlier
- is mostly silence, reading donations, sponsor reads, or talking to chat about logistics
- has no payoff inside the clip

Shape each clip:
- 20 to 60 seconds. Start on the sentence that sets it up, end right after the payoff lands, not three sentences later.
- Use the `start` of the first line and the `end` of the last line as times. The renderer snaps to word boundaries.
- Score 0 to 100: would a stranger who has never seen this streamer stop scrolling and watch to the end? Under 60 is not worth keeping.

Keep every candidate, then write them all to `.clipper/<id>/picks.json`:

```json
[
  {
    "start": 3984.5,
    "end": 4012.3,
    "score": 84,
    "category": "funny fail",
    "hook": "Tim loses the song game on a technicality",
    "caption": "He knew it was Free Fallin'. The game wanted the live version.",
    "hashtags": ["#timthetatman", "#gta6", "#gaming"],
    "take": "Honestly the live version should count"
  }
]
```

- `hook`: the on-screen title, under 60 characters, says what happens and names the streamer. No clickbait the clip does not deliver.
- `caption`: one or two lines for the post. `hashtags`: 3 to 6, streamer + game + one broad tag.
- `take` (optional): the channel's own one-line reaction, shown in place of the hook for the last 3 seconds. Under 60 characters, an opinion or punchline about what just happened, not a summary of it. It is what makes the clip the channel's edit rather than a straight re-upload, which matters for monetization, so write one for every pick the user is going to post.
- Only quote what the transcript says. Whisper mishears names; if a hook depends on a name or word you are unsure of, phrase it so it does not.

## 4. Render

```
clipper.py render .clipper/<id>            # top 3 by score
clipper.py render .clipper/<id> --top 5
clipper.py render .clipper/<id> --brand branding/logo.png   # channel logo as a small round watermark
clipper.py render .clipper/<id> --take-colors 111113,FF6229   # take card background,text in the channel's colors
```

If the user has a logo or brand colors (check for a `branding/` folder, or ask once), use them on every render.

For each pick it downloads only those seconds of video, finds the facecam, and writes to `clips/<id>/`:
- `NN_<hook>.mp4`: 1080x1920, facecam on top, gameplay below, hook title, word-by-word captions
- `NN_<hook>.txt`: hook, post caption, hashtags, source link with timestamp
- `NN_<hook>_preview.jpg`: one frame from the middle

## 5. Check before you report

Look at every `_preview.jpg` (and `.clipper/<id>/clipNN/facecam_check.jpg`, the source frame with the detected facecam box drawn in green).

- Layout printed `cam [x, y, w, h]`: the top panel should show the streamer and nothing else. The detector looks for a face that moves (so thumbnails and posters are ignored) and then for the camera's border. If the box is wrong, rerun with `--facecam x,y,w,h` (source pixels), or put `"facecam": [x, y, w, h]` on that pick.
- `fullcam`: the camera is the main picture (just chatting, or a camera wider than 40% of the frame). The clip is a single 9:16 crop that follows the streamer's face.
- On-screen alerts with a face in them (sub or member alert GIFs) can fool the detector for clips where the alert was showing. Compare `facecam_check.jpg` against the streamer's real camera overlay.
- `nocam`: no moving face found, the clip is a plain centre crop. If there is a facecam in the frame, pass `--facecam`. If there really is none, that is fine.
- Layouts change during a stream, so each clip is detected separately. Check every one.
- Captions come from Whisper and can mishear names and game titles. Read the caption words on each preview; if a wrong word is prominent, say so in your report. If the transcript is mostly garbled (common for Hindi and other non-English streams) or the captions show as empty boxes, rerun with `--no-captions`.

Then tell the user: how long the stream was, how many candidates you found, which ones you kept and why, and where the files are. Remind them to watch each clip before posting, and to clip streamers who allow it.
