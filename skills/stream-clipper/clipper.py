#!/usr/bin/env python3
"""Stream clipper: turn a long stream VOD into vertical, captioned short clips.

Subcommands
  find    list big recent streams for a search query
  prep    audio-only download -> local transcript + loudness + chat spikes -> 12-min windows
  render  read picks.json -> download only those minutes -> facecam-on-top vertical clips

The picking itself (reading the windows, writing picks.json) is done by the agent; see SKILL.md.
"""
import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

W, H = 1080, 1920          # output size
CAM_H = 608                # facecam panel height (16:9 at 1080 wide)
WINDOW = 720               # seconds of transcript per window file
CHUNK = 1200               # seconds of audio per transcription pass
SR = 16000


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def ytdlp(args, cookies=None, capture=False, timeout=None):
    # stderr is captured because section downloads go through ffmpeg, which floods the terminal
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist", "-q", "--no-progress"]
    if shutil.which("node"):
        cmd += ["--js-runtimes", "node"]
    if cookies:
        cmd += ["--cookies-from-browser", cookies]
    cmd += args
    return subprocess.run([str(c) for c in cmd], check=True, text=True, timeout=timeout,
                          stdout=subprocess.PIPE if capture else subprocess.DEVNULL, stderr=subprocess.PIPE)


def hms(t):
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:50] or "clip"


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True).stdout
    j = json.loads(out)
    st = (j.get("streams") or [{}])[0]
    return st.get("width"), st.get("height"), float(j["format"]["duration"])


# ----------------------------------------------------------------------------- find

def cmd_find(a):
    from urllib.parse import quote_plus
    if a.channel:
        # a channel's past broadcasts, newest first
        ch = a.channel.lstrip("@")
        url = {"youtube": f"https://www.youtube.com/@{ch}/streams",
               "twitch": f"https://www.twitch.tv/{ch}/videos?filter=archives&sort=time",
               "kick": f"https://kick.com/{ch}/videos"}[a.platform]
    elif a.platform == "youtube":
        if not a.query:
            sys.exit("Give a search query, or --channel NAME.")
        sp = {"today": "EgIIAg%3D%3D", "week": "EgIIAw%3D%3D", "month": "EgIIBA%3D%3D"}[a.when]
        url = f"https://www.youtube.com/results?search_query={quote_plus(a.query)}&sp={sp}"
    else:
        sys.exit(f"Searching {a.platform} by topic is not supported. Name a streamer: "
                 f"find --platform {a.platform} --channel NAME")
    out = ytdlp(["--flat-playlist", "--playlist-end", a.n, "-J", url], a.cookies, capture=True).stdout
    rows, seen = [], set()
    for e in json.loads(out).get("entries") or []:
        dur = e.get("duration") or 0
        live = e.get("is_live") or e.get("live_status") == "is_live"
        if dur < a.min_minutes * 60 and not live:
            continue
        if a.channel and a.query and not all(w in (e.get("title") or "").lower() for w in a.query.lower().split()):
            continue
        link = e.get("webpage_url") or e.get("url") or ""
        if e.get("ie_key") == "Youtube" and not link.startswith("http"):
            link = f"https://www.youtube.com/watch?v={e['id']}"
        link = link.split("#")[0]
        if link in seen:
            continue
        seen.add(link)
        rows.append((e.get("view_count") or 0, dur, e.get("channel") or e.get("uploader") or a.channel or "?",
                     (e.get("title") or "?") + ("  [LIVE NOW]" if live else ""), link))
    if not a.channel:
        rows.sort(reverse=True)
    for views, dur, ch, title, link in rows[:a.top]:
        print(f"{views:>10,} views  {hms(dur)}  {ch}  |  {title}\n            {link}")
    if not rows:
        print(f"No past broadcasts found for {a.channel} on {a.platform}. Check the spelling of the channel name."
              if a.channel else
              "Nothing long enough matched. Try a different query, --when week, or a lower --min-minutes.")


# ----------------------------------------------------------------------------- prep

def pcm(audio, start=None, dur=None):
    """Decode (part of) the audio file to 16 kHz mono s16 PCM bytes on stdout."""
    cmd = ["ffmpeg", "-v", "error"]
    if start is not None:
        cmd += ["-ss", start, "-t", dur]
    cmd += ["-i", audio, "-vn", "-ac", 1, "-ar", SR, "-f", "s16le", "-"]
    return subprocess.Popen([str(c) for c in cmd], stdout=subprocess.PIPE)


def loudness(audio):
    """RMS level in dB for every second of the file."""
    import numpy as np
    p = pcm(audio)
    out = []
    while True:
        buf = p.stdout.read(SR * 2)
        if len(buf) < SR:
            break
        x = np.frombuffer(buf[:len(buf) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768
        out.append(round(float(20 * np.log10(max(np.sqrt(np.mean(x * x)), 1e-5))), 1))
    p.wait()
    return out


def pick_model(name):
    if name != "auto":
        return name
    try:
        gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except (ValueError, OSError, AttributeError):
        gb = 8
    return "large-v3-turbo" if gb >= 16 else "small"


def make_transcriber(model):
    """Return f(float32 array) -> [{start,end,text,words:[{start,end,word}]}], local only."""
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        import mlx_whisper
        repo = model if "/" in model else (
            "mlx-community/whisper-large-v3-turbo" if model == "large-v3-turbo"
            else f"mlx-community/whisper-{model}-mlx")

        def f(x):
            r = mlx_whisper.transcribe(x, path_or_hf_repo=repo, word_timestamps=True,
                                       condition_on_previous_text=False, verbose=None)
            return r["segments"]
        return f
    from faster_whisper import WhisperModel
    m = WhisperModel(model, compute_type="int8")

    def f(x):
        segs, _ = m.transcribe(x, word_timestamps=True, condition_on_previous_text=False)
        return [{"start": s.start, "end": s.end, "text": s.text,
                 "words": [{"start": w.start, "end": w.end, "word": w.word} for w in s.words or []]}
                for s in segs]
    return f


def transcribe(audio, dur, offset, model, out_path):
    import numpy as np
    state = json.loads(out_path.read_text()) if out_path.exists() else {"chunks_done": 0, "segments": []}
    n = int(-(-dur // CHUNK))
    if state["chunks_done"] >= n:
        return state["segments"]
    f = make_transcriber(model)
    for i in range(state["chunks_done"], n):
        p = pcm(audio, i * CHUNK, CHUNK)
        x = np.frombuffer(p.stdout.read(), dtype=np.int16).astype(np.float32) / 32768
        p.wait()
        base = offset + i * CHUNK
        for s in f(x) if len(x) > SR else []:
            text = s["text"].strip()
            if not text:
                continue
            state["segments"].append({
                "start": round(base + s["start"], 2), "end": round(base + s["end"], 2), "text": text,
                "words": [{"start": round(base + w["start"], 2), "end": round(base + w["end"], 2),
                           "word": w["word"].strip()} for w in s.get("words") or [] if w["word"].strip()]})
        state["chunks_done"] = i + 1
        out_path.write_text(json.dumps(state))
        print(f"  transcribed {min((i + 1) * CHUNK, int(dur)) // 60}/{int(dur) // 60} min", flush=True)
    return state["segments"]


def chat_rate(url, work, cookies, bucket=10):
    """Messages per `bucket` seconds from a YouTube chat replay. {} if unavailable."""
    f = work / "chat.live_chat.json"
    if not f.exists():
        try:
            ytdlp(["--skip-download", "--write-subs", "--sub-langs", "live_chat",
                   "-o", work / "chat", url], cookies, timeout=1500)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
    if not f.exists():
        return {}
    counts = {}
    for line in f.open(errors="ignore"):
        try:
            ms = json.loads(line)["replayChatItemAction"]["videoOffsetTimeMsec"]
        except (ValueError, KeyError):
            continue
        b = int(ms) // 1000 // bucket
        counts[b] = counts.get(b, 0) + 1
    return counts


def cmd_prep(a):
    import numpy as np
    info = json.loads(ytdlp(["-J", a.url], a.cookies, capture=True).stdout)
    vid = info["id"]
    live = info.get("is_live") or info.get("live_status") == "is_live"
    if live and info.get("extractor_key") == "Youtube":
        sys.exit("This YouTube stream is still live. Wait for the replay to be published, then run prep again.")
    if live:
        print("Stream is still live: clipping what has been broadcast so far.")
    work = Path(a.root) / ".clipper" / vid
    work.mkdir(parents=True, exist_ok=True)
    offset = a.start or 0
    end = a.end or info.get("duration") or 0
    meta = {"url": a.url, "id": vid, "title": info.get("title"), "channel": info.get("channel") or info.get("uploader"),
            "duration": info.get("duration"), "extractor": info.get("extractor_key"), "offset": offset,
            "range": [a.start, a.end]}
    mf = work / "meta.json"
    if mf.exists() and json.loads(mf.read_text()).get("range", [None, None]) != meta["range"]:
        # a different part of the stream was asked for: the old audio and transcript do not apply
        for f in [*work.glob("audio.*"), work / "loudness.json", work / "transcript.json"]:
            f.unlink(missing_ok=True)
    mf.write_text(json.dumps(meta, indent=1))
    print(f"{meta['channel']} | {meta['title']} | {hms(meta['duration'] or 0)}\nwork dir: {work}")

    audio = next(iter(work.glob("audio.*")), None)
    if not audio:
        print("1/4 downloading audio only...")
        # sites without an audio-only track (Kick) fall back to the smallest video
        args = ["-f", "bestaudio/worst", "-o", work / "audio.%(ext)s", a.url]
        if a.start or a.end:
            args = ["--download-sections", f"*{offset}-{end or 'inf'}"] + args
        ytdlp(args, a.cookies)
        audio = next(iter(work.glob("audio.*")))
    dur = probe(audio)[2]

    print("2/4 measuring loudness...")
    lf = work / "loudness.json"
    db = json.loads(lf.read_text()) if lf.exists() else loudness(audio)
    if not db:
        sys.exit("The downloaded audio is empty. Check the link and the --start/--end range.")
    lf.write_text(json.dumps(db))
    arr = np.array(db)
    live = arr[arr > -50] if (arr > -50).any() else arr
    p95, p99 = np.percentile(live, 95), np.percentile(live, 99)

    model = pick_model(a.model)
    print(f"3/4 transcribing locally with whisper {model}...")
    segs = transcribe(audio, dur, offset, model, work / "transcript.json")

    chat, cmed, cthr = {}, 0, 0
    if meta["extractor"] == "Youtube" and not a.no_chat:
        print("4/4 fetching chat replay (a busy 3-hour chat takes about 10 minutes; --no-chat skips it)...")
        chat = chat_rate(a.url, work, a.cookies)
        if chat:
            # busy chats are rate-limited and never double, so a spike is "top 5% of this stream"
            rates = list(chat.values())
            cmed = float(np.median(rates))
            cthr = max(1.5 * cmed, float(np.percentile(rates, 95)))
        else:
            print("    this stream has no chat replay, using transcript and loudness only")
    else:
        print("4/4 chat replay skipped (only YouTube replays provide one)")

    def tags(s):
        i0, i1 = int(s["start"] - offset), int(s["end"] - offset) + 1
        peak = max(db[i0:i1], default=-99)
        t = " {LOUD!!}" if peak >= p99 else " {LOUD}" if peak >= p95 else ""
        if cmed:
            # chat reacts a few seconds late
            r = max((chat.get(b, 0) for b in range(int(s["start"]) // 10, int(s["end"] + 15) // 10 + 1)), default=0)
            if r >= cthr:
                t += f" {{CHAT x{r / cmed:.1f}}}"
        return t

    wdir = work / "windows"
    shutil.rmtree(wdir, ignore_errors=True)
    wdir.mkdir()
    files = {}
    for s in segs:
        k = int((s["start"] - offset) // WINDOW)
        files.setdefault(k, []).append(f"[{s['start']:.1f}-{s['end']:.1f} | {hms(s['start'])}] {s['text']}{tags(s)}")
    for k, lines in sorted(files.items()):
        head = (f"# {meta['channel']} - {meta['title']}\n# window {k + 1}: {hms(offset + k * WINDOW)} to "
                f"{hms(offset + (k + 1) * WINDOW)}. Times are seconds into the stream.\n"
                f"# {{LOUD}} = streamer in loudest 5%, {{LOUD!!}} = loudest 1%, {{CHAT xN}} = chat in its busiest 5%, N times its usual speed\n\n")
        (wdir / f"w{k + 1:03d}.txt").write_text(head + "\n".join(lines) + "\n")
    if not files:
        print("\nNo speech was found, so there is nothing to pick from.")
        return
    print(f"\nDone. {len(files)} window files in {wdir}\nNext: read them, write {work / 'picks.json'}, then run render.")


# ----------------------------------------------------------------------------- render

def cam_edges(grays, cx, cy, w, fw, fh):
    """Find the facecam's border around a face: overlay borders are straight edges that stay put while
    the picture on both sides changes, so they survive averaging the edge maps of many frames."""
    import cv2
    import numpy as np
    ex = np.mean([np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0)) for g in grays], axis=0)
    ey = np.mean([np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1)) for g in grays], axis=0)

    def walk(profile, start, stop, step, edge):
        # nearest line (walking away from the face) that is strong along most of its length
        thr = max(25.0, 4 * float(np.median(profile)))
        for i in range(int(start), int(stop), step):
            if profile[i] > thr and profile[i] >= profile[max(i - 2, 0):i + 3].max():
                return i
        return edge

    y0, y1 = int(max(cy - w, 0)), int(min(cy + w, fh))
    col = np.percentile(ex[y0:y1], 25, axis=0)
    left = walk(col, cx - w, max(cx - 12 * w, 0), -1, 0)
    right = walk(col, cx + w, min(cx + 12 * w, fw - 1), 1, fw)
    x0, x1 = int(left + (right - left) * 0.1), int(right - (right - left) * 0.1)
    row = np.percentile(ey[:, x0:x1], 25, axis=1)
    top = walk(row, cy - w, max(cy - 8 * w, 0), -1, 0)
    bottom = walk(row, cy + w, min(cy + 10 * w, fh - 1), 1, fh)
    bw, bh = right - left, bottom - top
    if bw < 2 * w or bh < 1.5 * w or bw > fw * 0.6 or bh > fh * 0.7 or not 0.6 < bw / bh < 2.4:
        return None
    return [left, top, bw, bh]


def find_facecam(video, fw, fh, debug=None):
    """Locate the streamer's face. Returns ('cam', [x,y,w,h]) | ('fullcam', face_cx) | ('nocam', None)."""
    import cv2
    import numpy as np
    model = Path(__file__).with_name("face_yunet.onnx")
    if model.exists():
        yn = cv2.FaceDetectorYN.create(str(model), "", (fw, fh), 0.6)

        def faces(frame):
            r = yn.detect(frame)[1]
            return [] if r is None else [f[:4] for f in r]
    else:
        haar = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        m = int(fh * 0.03)

        def faces(frame):
            return haar.detectMultiScale(cv2.equalizeHist(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)), 1.1, 6, minSize=(m, m))
    cap = cv2.VideoCapture(str(video))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    samples, grays, found, frame = 16, [], [], None
    for i in range(samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (i + 0.5) / samples))
        ok, img = cap.read()
        if not ok:
            continue
        frame = img
        grays.append(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
        found.append([(x + w / 2, y + h / 2, w, h) for (x, y, w, h) in faces(img)])
    cap.release()
    if len(grays) < 2:
        return "nocam", None

    clusters = []
    for i, boxes in enumerate(found):
        for c in boxes:
            for cl in clusters:
                mx, my, mw, _ = np.mean(cl["boxes"], axis=0)
                if abs(c[0] - mx) < mw * 1.5 and abs(c[1] - my) < mw * 1.5:
                    cl["boxes"].append(c)
                    cl["frames"].add(i)
                    break
            else:
                clusters.append({"boxes": [c], "frames": {i}})

    # A streamer moves; faces in thumbnails, posters and web pages do not. Keep faces whose pixels change.
    change = np.mean([cv2.absdiff(a, b).astype(np.float32) for a, b in zip(grays, grays[1:])], axis=0)
    live = []
    for cl in clusters:
        if len(cl["frames"]) < len(grays) * 0.3:
            continue
        cx, cy, w, h = np.median(cl["boxes"], axis=0)
        patch = change[int(max(cy - h / 2, 0)):int(cy + h / 2) + 1, int(max(cx - w / 2, 0)):int(cx + w / 2) + 1]
        cl["motion"] = float(patch.mean()) if patch.size else 0.0
        if cl["motion"] >= 8:
            live.append(cl)
    if not live:
        return "nocam", None
    best = max(live, key=lambda c: len(c["frames"]) * c["motion"])
    cx, cy, w, _ = np.median(best["boxes"], axis=0)
    if w > fw * 0.22:
        return "fullcam", float(cx)
    box = cam_edges(grays, cx, cy, w, fw, fh)
    if (box[2] if box else w * 4.5) > fw * 0.4:
        # a camera this big is the main picture, not an overlay: showing it twice looks wrong
        return "fullcam", float(cx)
    if box:
        # the panel is 16:9: take the widest 16:9 piece of the camera, keeping the face in it
        x, y, bw, bh = box
        cw = min(bw, bh * W / CAM_H)
        ch = cw * CAM_H / W
        box = [min(max(cx - cw / 2, x), x + bw - cw), min(max(cy - ch * 0.45, y), y + bh - ch), cw, ch]
    else:
        # no visible border: guess a box around the face
        cw = min(fw, w * 4.5)
        ch = cw * CAM_H / W
        box = [min(max(cx - cw / 2, 0), fw - cw), min(max(cy - ch * 0.45, 0), fh - ch), cw, ch]
    box = [int(v) // 2 * 2 for v in box]
    if debug:
        cv2.rectangle(frame, (box[0], box[1]), (box[0] + box[2], box[1] + box[3]), (0, 255, 0), 4)
        cv2.imwrite(str(debug), cv2.resize(frame, (fw // 2, fh // 2)))
    return "cam", box


def load_font(size):
    from PIL import ImageFont
    for p in ("/System/Library/Fonts/Supplemental/Arial Black.ttf", "/System/Library/Fonts/Supplemental/Impact.ttf",
              "/Library/Fonts/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def title_png(text, y_center, path):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    size = 56
    while True:
        font = load_font(size)
        lines, cur = [], ""
        for word in text.split():
            t = f"{cur} {word}".strip()
            if d.textlength(t, font=font) > W - 160 and cur:
                lines.append(cur)
                cur = word
            else:
                cur = t
        lines.append(cur)
        if len(lines) <= 3 or size <= 36:
            break
        size -= 4
    lh = int(size * 1.25)
    bw = int(max(d.textlength(l, font=font) for l in lines)) + 64
    bh = lh * len(lines) + 36
    x0, y0 = (W - bw) // 2, max(20, y_center - bh // 2)
    d.rounded_rectangle([x0, y0, x0 + bw, y0 + bh], radius=22, fill=(255, 255, 255, 255))
    for i, l in enumerate(lines):
        d.text((W // 2, y0 + 18 + i * lh + lh // 2), l, font=font, fill=(0, 0, 0, 255), anchor="mm")
    img.save(path)


STRIP_H = 220


def caption_png(words, active, path):
    """One caption line with the word being spoken highlighted."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (W, STRIP_H), (0, 0, 0, 0))
    if words:
        d = ImageDraw.Draw(img)
        size = 84
        while True:
            font = load_font(size)
            total = d.textlength(" ".join(words), font=font)
            if total <= W - 100 or size <= 44:
                break
            size -= 6
        x, space = (W - total) / 2, d.textlength(" ", font=font)
        for i, w in enumerate(words):
            d.text((x, STRIP_H // 2), w, font=font, anchor="lm", stroke_width=size // 9, stroke_fill=(0, 0, 0, 255),
                   fill=(255, 230, 0, 255) if i == active else (255, 255, 255, 255))
            x += d.textlength(w, font=font) + space
    img.save(path)


def build_captions(words, start, dur, tmp):
    """Word-by-word caption frames as an ffmpeg concat list. `words` are absolute-time."""
    ws = [{"s": max(w["start"] - start, 0), "e": min(w["end"] - start, dur), "w": w["word"].upper()}
          for w in words if w["end"] > start and w["start"] < start + dur]
    groups, cur = [], []
    for w in ws:
        if cur and (len(cur) == 3 or w["s"] - cur[-1]["e"] > 0.6 or cur[-1]["w"][-1] in ".?!"
                    or sum(len(c["w"]) for c in cur) + len(w["w"]) > 18):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)

    blank = tmp / "cap_blank.png"
    caption_png([], -1, blank)
    events, t = [], 0.0
    for gi, g in enumerate(groups):
        nxt = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else dur
        for i, w in enumerate(g):
            s = max(w["s"], t)
            e = g[i + 1]["s"] if i + 1 < len(g) else min(w["e"] + 0.3, nxt)
            if e - s < 0.03:
                continue
            if s - t >= 0.03:
                events.append((blank, s - t))
            png = tmp / f"cap_{gi}_{i}.png"
            caption_png([c["w"] for c in g], i, png)
            events.append((png, e - s))
            t = e
    events.append((blank, max(dur - t, 0.04)))
    lst = tmp / "caps.txt"
    def q(p):
        return str(p).replace("'", "'\\''")
    lst.write_text("".join(f"file '{q(p)}'\nduration {d:.3f}\n" for p, d in events) + f"file '{q(blank)}'\n")
    return lst


def encoder():
    enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if "libx264" in enc:
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    return ["-c:v", "h264_videotoolbox", "-b:v", "8M"]


def load_picks(path, top):
    """Best `top` picks by score. Overlapping picks are the same moment found twice: keep the better one."""
    try:
        picks = json.loads(path.read_text())
    except FileNotFoundError:
        sys.exit(f"{path} does not exist. Write the picks first (format in SKILL.md).")
    except ValueError as e:
        sys.exit(f"{path} is not valid JSON: {e}")
    if not isinstance(picks, list) or not picks:
        sys.exit(f"{path} must be a non-empty list of picks.")
    for i, p in enumerate(picks, 1):
        if not isinstance(p, dict) or not isinstance(p.get("start"), (int, float)) \
                or not isinstance(p.get("end"), (int, float)) or not str(p.get("hook") or "").strip():
            sys.exit(f"Pick {i} needs numeric start and end, and a hook.")
        if p["end"] <= p["start"]:
            sys.exit(f"Pick {i}: end ({p['end']}) must be after start ({p['start']}).")
    kept = []
    for p in sorted(picks, key=lambda p: -p.get("score", 0)):
        if all(p["end"] <= k["start"] or p["start"] >= k["end"] for k in kept):
            kept.append(p)
    return kept[:top]


def cmd_render(a):
    work = Path(a.work).resolve()
    if not (work / "transcript.json").exists():
        sys.exit(f"{work} has no transcript. Run prep first and pass the work dir it prints.")
    meta = json.loads((work / "meta.json").read_text())
    segs = json.loads((work / "transcript.json").read_text())["segments"]
    words = [w for s in segs for w in s["words"]]
    picks = load_picks(work / "picks.json", a.top)
    out_dir = Path(a.out or Path.cwd() / "clips" / meta["id"])
    out_dir.mkdir(parents=True, exist_ok=True)
    enc = encoder()

    for n, p in enumerate(picks, 1):
        # snap to word boundaries so the clip never starts or ends mid-word
        inside = [w for w in words if w["end"] > p["start"] - 0.3 and w["start"] < p["end"] + 0.3]
        start = max((inside[0]["start"] if inside else p["start"]) - 0.2, 0)
        end = (inside[-1]["end"] if inside else p["end"]) + 0.4
        dur = end - start
        tmp = work / f"clip{n:02d}"
        tmp.mkdir(exist_ok=True)

        raw = next(iter(tmp.glob("raw.*")), None)
        a0 = max(start - 2, 0)
        cut = tmp / "cut.json"
        if not raw or not cut.exists() or json.loads(cut.read_text()) != [a0, end + 2]:
            for f in tmp.glob("raw.*"):
                f.unlink()
            print(f"[{n}/{len(picks)}] downloading {hms(start)}-{hms(end)} ({dur:.0f}s)...")
            ytdlp(["-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--download-sections", f"*{a0}-{end + 2}",
                   "--force-keyframes-at-cuts", "--merge-output-format", "mp4", "-o", tmp / "raw.%(ext)s",
                   meta["url"]], a.cookies)
            raw = next(iter(tmp.glob("raw.*")))
            cut.write_text(json.dumps([a0, end + 2]))
        fw, fh, _ = probe(raw)

        cam = p.get("facecam") or a.facecam
        if a.layout == "nocam":
            mode, box = "nocam", None
        elif cam:
            mode, box = "cam", [int(v) for v in (cam.split(",") if isinstance(cam, str) else cam)]
        else:
            mode, box = find_facecam(raw, fw, fh, tmp / "facecam_check.jpg")
        print(f"    layout: {mode} {box if box is not None else ''}")

        if mode == "cam":
            gh = H - CAM_H
            gw = int(fh * W / gh) // 2 * 2
            base = (f"[0:v]fps=30,split[a][b];[a]crop={box[2]}:{box[3]}:{box[0]}:{box[1]},scale={W}:{CAM_H}:force_original_aspect_ratio=increase,crop={W}:{CAM_H},setsar=1[top];"
                    f"[b]crop={gw}:{fh}:{(fw - gw) // 2}:0,scale={W}:{gh},setsar=1[bot];[top][bot]vstack[base]")
            title_y, cap_y = CAM_H, CAM_H + int(gh * 0.52)
        else:
            cw = int(fh * 9 / 16) // 2 * 2
            cx = (fw - cw) // 2 if mode == "nocam" else int(min(max(box - cw / 2, 0), fw - cw))
            base = f"[0:v]fps=30,crop={cw}:{fh}:{cx}:0,scale={W}:{H},setsar=1[base]"
            title_y, cap_y = 250, int(H * 0.66)

        title_png(p["hook"], title_y, tmp / "title.png")
        caps = build_captions(words, start, dur, tmp)
        name = f"{n:02d}_{slugify(p['hook'])}"
        out = out_dir / f"{name}.mp4"
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start - a0:.3f}", "-t", f"{dur:.3f}", "-i", raw,
             "-i", tmp / "title.png", "-f", "concat", "-safe", 0, "-i", caps,
             "-filter_complex",
             f"{base};[base][1:v]overlay=0:0[t];[2:v]fps=30,format=rgba[c];[t][c]overlay=0:{cap_y}[v]",
             "-map", "[v]", "-map", "0:a?", "-t", f"{dur:.3f}", *enc, "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out])
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{dur / 2:.2f}", "-i", out, "-frames:v", 1,
             out_dir / f"{name}_preview.jpg"])
        t = int(start)
        stamp = {"Youtube": f"t={t}s", "TwitchVod": f"t={t // 3600}h{t % 3600 // 60}m{t % 60}s"}.get(meta["extractor"])
        src = f"{meta['url']}{'&' if '?' in meta['url'] else '?'}{stamp}" if stamp else meta["url"]
        (out_dir / f"{name}.txt").write_text(
            f"{p['hook']}\n\n{p.get('caption', '')}\n\n{' '.join(p.get('hashtags', []))}\n\n"
            f"Source: {meta['channel']} - {src} ({hms(start)}-{hms(end)}), score {p.get('score', '?')}\n")
        print(f"    -> {out}")
    print(f"\n{len(picks)} clips in {out_dir}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cookies", help="browser to borrow login cookies from (chrome, safari, firefox...) "
                                      "for age-restricted or login-only streams")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("find", help="list big recent streams for a query")
    f.add_argument("query", nargs="?", help="topic to search (YouTube), or a title filter when --channel is given")
    f.add_argument("--platform", choices=["youtube", "twitch", "kick"], default="youtube")
    f.add_argument("--channel", help="list this streamer's past broadcasts instead of searching")
    f.add_argument("-n", type=int, default=40, help="search results to scan")
    f.add_argument("--top", type=int, default=10)
    f.add_argument("--when", choices=["today", "week", "month"], default="today", help="upload date filter")
    f.add_argument("--min-minutes", type=int, default=30)
    f.set_defaults(fn=cmd_find)

    p = sub.add_parser("prep", help="download audio, transcribe, mark loud/chat spikes, write windows")
    p.add_argument("url")
    p.add_argument("--root", default=".", help="where the .clipper work folder goes")
    p.add_argument("--model", default="auto", help="whisper size: base, small, medium, large-v3-turbo (auto picks by RAM)")
    p.add_argument("--start", type=float, help="only process from this second (testing / partial runs)")
    p.add_argument("--end", type=float, help="only process up to this second")
    p.add_argument("--no-chat", action="store_true")
    p.set_defaults(fn=cmd_prep)

    r = sub.add_parser("render", help="render the picks in <work>/picks.json")
    r.add_argument("work", help="work dir printed by prep (.clipper/<id>)")
    r.add_argument("--top", type=int, default=3, help="how many of the highest-scored picks to render")
    r.add_argument("--out", help="output folder (default ./clips/<id>)")
    r.add_argument("--layout", choices=["auto", "nocam"], default="auto")
    r.add_argument("--facecam", help="x,y,w,h of the facecam in source pixels, overrides detection")
    r.set_defaults(fn=cmd_render)

    a = ap.parse_args()
    try:
        a.fn(a)
    except subprocess.CalledProcessError as e:
        sys.exit(f"command failed ({e.returncode}): {' '.join(map(str, e.cmd))[:300]}\n{(e.stderr or '')[-1500:]}")


if __name__ == "__main__":
    main()
