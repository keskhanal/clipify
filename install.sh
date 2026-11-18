#!/bin/bash
# Link the stream-clipper skill into ~/.claude/skills and build its Python environment.
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
SRC="$REPO/skills/stream-clipper"
DEST="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}/stream-clipper"

mkdir -p "$(dirname "$DEST")"
if [ -e "$DEST" ] && [ ! -L "$DEST" ]; then
  echo "$DEST already exists and is not a link. Move it away and run this again." >&2
  exit 1
fi
ln -sfn "$SRC" "$DEST"
echo "Linked $DEST -> $SRC"

"$SRC/setup.sh"
echo "Open Claude Code and say: clip this stream <link>"
