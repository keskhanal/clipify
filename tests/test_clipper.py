"""Offline tests: no network, no Whisper. Needs ffmpeg plus the packages from setup.sh.

Run:  skills/stream-clipper/.venv/bin/python -m unittest discover -s tests -v
"""
import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "stream-clipper"))
import clipper  # noqa: E402


def words(*items):
    return [{"start": s, "end": e, "word": w} for s, e, w in items]


class Helpers(unittest.TestCase):
    def test_hms(self):
        self.assertEqual(clipper.hms(0), "0:00:00")
        self.assertEqual(clipper.hms(3984.5), "1:06:24")

    def test_slugify(self):
        self.assertEqual(clipper.slugify("Tim CAN'T believe it!!"), "tim-can-t-believe-it")
        self.assertEqual(clipper.slugify("🔥🔥"), "clip")
        self.assertLessEqual(len(clipper.slugify("x" * 200)), 50)


class Finished(unittest.TestCase):
    def test_ignores_partial_downloads(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "audio.mp4.part").write_bytes(b"x")
            self.assertIsNone(clipper.finished(d, "audio"))
            (d / "audio.webm").write_bytes(b"x")
            self.assertEqual(clipper.finished(d, "audio").name, "audio.webm")


class Picks(unittest.TestCase):
    def load(self, data, top=3):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "picks.json"
            f.write_text(data if isinstance(data, str) else json.dumps(data))
            return clipper.load_picks(f, top)

    def test_top_by_score(self):
        picks = [{"start": i * 100, "end": i * 100 + 30, "hook": f"h{i}", "score": s}
                 for i, s in enumerate([50, 90, 70, 80])]
        self.assertEqual([p["score"] for p in self.load(picks)], [90, 80, 70])

    def test_overlap_keeps_better(self):
        picks = [{"start": 10, "end": 40, "hook": "a", "score": 60},
                 {"start": 30, "end": 70, "hook": "b", "score": 90},
                 {"start": 70, "end": 90, "hook": "c", "score": 50}]
        self.assertEqual([p["hook"] for p in self.load(picks)], ["b", "c"])

    def test_rejects_bad_input(self):
        for bad in ("not json", [], [{"start": 5, "end": 9}], [{"start": 9, "end": 5, "hook": "x"}],
                    [{"start": "1", "end": 5, "hook": "x"}]):
            with self.assertRaises(SystemExit, msg=repr(bad)):
                self.load(bad)

    def test_missing_file(self):
        with self.assertRaises(SystemExit):
            clipper.load_picks(Path("/nonexistent/picks.json"), 3)


class Captions(unittest.TestCase):
    def events(self, ws, start, dur):
        with tempfile.TemporaryDirectory() as d:
            lst = clipper.build_captions(ws, start, dur, Path(d))
            lines = lst.read_text().splitlines()
            for l in lines:
                if l.startswith("file "):
                    self.assertTrue(Path(l[6:-1]).exists(), l)
            return [float(l.split()[1]) for l in lines if l.startswith("duration")]

    def test_timeline_covers_clip_exactly(self):
        ws = words((100.2, 100.5, "Oh"), (100.5, 100.9, "my"), (100.9, 101.4, "god."), (103.0, 103.4, "What"),
                   (103.4, 103.6, "is"), (103.6, 104.1, "that?"), (104.1, 104.2, "a"), (104.15, 104.3, "b"))
        durs = self.events(ws, 100.0, 6.0)
        self.assertTrue(all(d > 0 for d in durs))
        self.assertAlmostEqual(sum(durs), 6.0, places=1)

    def test_only_words_inside_clip(self):
        ws = words((1, 2, "before"), (10.2, 10.6, "inside"), (50, 51, "after"))
        with tempfile.TemporaryDirectory() as d:
            clipper.build_captions(ws, 10.0, 5.0, Path(d))
            self.assertEqual(len(list(Path(d).glob("cap_[0-9]*.png"))), 1)

    def test_no_words(self):
        self.assertAlmostEqual(sum(self.events([], 0.0, 4.0)), 4.0, places=2)

    def test_images(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            clipper.caption_png(["SUPERCALIFRAGILISTIC", "EXPIALIDOCIOUS", "WORD"], 1, Path(d) / "c.png")
            self.assertEqual(Image.open(Path(d) / "c.png").size, (clipper.W, clipper.STRIP_H))
            clipper.title_png("A very long hook title " * 6, clipper.CAM_H, Path(d) / "t.png")
            img = Image.open(Path(d) / "t.png")
            self.assertEqual(img.size, (clipper.W, clipper.H))
            self.assertIsNotNone(img.getbbox())


class CamEdges(unittest.TestCase):
    def test_finds_bordered_box(self):
        import numpy as np
        rng = np.random.default_rng(0)
        grays = []
        for _ in range(8):
            g = rng.integers(0, 60, (720, 1280), dtype=np.uint8)           # dark, changing gameplay
            g[60:300, 900:1260] = rng.integers(120, 200, (240, 360), dtype=np.uint8)  # brighter, changing cam
            grays.append(g)
        x, y, w, h = clipper.cam_edges(grays, cx=1080, cy=160, w=50, fw=1280, fh=720)
        self.assertLess(abs(x - 900), 6)
        self.assertLess(abs(y - 60), 6)
        self.assertLess(abs(w - 360), 10)
        self.assertLess(abs(h - 240), 10)

    def test_no_border_returns_none(self):
        import numpy as np
        rng = np.random.default_rng(1)
        grays = [rng.integers(0, 255, (720, 1280), dtype=np.uint8) for _ in range(8)]
        self.assertIsNone(clipper.cam_edges(grays, cx=640, cy=360, w=50, fw=1280, fh=720))


class Render(unittest.TestCase):
    """Runs the real ffmpeg graph on a generated source clip, so no download happens."""

    def render(self, **opts):
        with tempfile.TemporaryDirectory() as d:
            work, out = Path(d) / "work", Path(d) / "out"
            (work / "clip01").mkdir(parents=True)
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=12",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=12", "-c:v", "mpeg4", "-q:v", "5",
                            "-c:a", "aac", "-shortest", str(work / "clip01" / "raw.mp4")], check=True)
            ws = words((103.0, 103.5, "This"), (103.5, 104.0, "is"), (104.0, 104.8, "a"), (104.8, 105.5, "test."),
                       (106.0, 107.0, "It's"), (107.0, 108.0, "working!"))
            (work / "transcript.json").write_text(json.dumps(
                {"segments": [{"start": 103.0, "end": 108.0, "text": "", "words": ws}]}))
            (work / "meta.json").write_text(json.dumps(
                {"url": "https://www.twitch.tv/videos/1", "id": "t", "channel": "Tester", "extractor": "TwitchVod"}))
            (work / "picks.json").write_text(json.dumps(
                [{"start": 103.0, "end": 108.0, "score": 80, "hook": "It's a test", "caption": "c", "hashtags": ["#t"]}]))
            # what render would have downloaded: start 102.8 minus 2 s padding, end 108.4 plus 2 s
            (work / "clip01" / "cut.json").write_text(json.dumps([100.8, 110.4]))
            clipper.cmd_render(argparse.Namespace(work=str(work), top=3, out=str(out), layout=opts.get("layout", "auto"),
                                                  facecam=opts.get("facecam"), cookies=None))
            mp4 = next(out.glob("*.mp4"))
            w, h, dur = clipper.probe(mp4)
            return w, h, dur, next(out.glob("*.txt")).read_text(), len(list(out.glob("*_preview.jpg")))

    def test_facecam_layout(self):
        w, h, dur, txt, previews = self.render(facecam="900,40,360,200")
        self.assertEqual((w, h), (1080, 1920))
        self.assertAlmostEqual(dur, 5.6, delta=0.2)
        self.assertIn("It's a test", txt)
        self.assertIn("?t=0h1m42s", txt)
        self.assertEqual(previews, 1)

    def test_nocam_layout(self):
        w, h, dur, _, _ = self.render(layout="nocam")
        self.assertEqual((w, h), (1080, 1920))
        self.assertAlmostEqual(dur, 5.6, delta=0.2)

    def test_auto_layout_without_a_face(self):
        self.assertEqual(self.render()[:2], (1080, 1920))


if __name__ == "__main__":
    unittest.main()
