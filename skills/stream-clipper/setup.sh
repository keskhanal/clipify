#!/bin/bash
# One-time setup for the stream clipper: checks ffmpeg and builds a private Python env next to this file.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v ffmpeg >/dev/null; then
  if command -v brew >/dev/null; then
    brew install ffmpeg
  else
    echo "ffmpeg is missing. Install it (https://ffmpeg.org/download.html) and run this again." >&2
    exit 1
  fi
fi

PY=$(command -v python3.12 || command -v python3.11 || command -v python3)
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q --upgrade "yt-dlp[default]" numpy pillow "opencv-python-headless<5"

if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
  .venv/bin/pip install -q mlx-whisper
else
  .venv/bin/pip install -q faster-whisper
fi

# small face-detection model used to find the facecam
[ -s face_yunet.onnx ] || curl -sfL -o face_yunet.onnx \
  https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx \
  || echo "Note: could not download the face model, falling back to a weaker detector."

command -v node >/dev/null || echo "Note: node not found. YouTube downloads work better with it (brew install node)."
echo "Stream clipper is ready."
