#!/usr/bin/env sh
# Install the LitVault skill without the plugin system: copies skills/lit-digest into ~/.claude/skills/.
# Usage:  sh install.sh            (from a clone of this repo)
#         sh install.sh --project  (install into ./.claude/skills of the current folder instead)
set -e
SRC="$(cd "$(dirname "$0")" && pwd)/skills/lit-digest"
if [ "$1" = "--project" ]; then DEST="$(pwd)/.claude/skills/lit-digest"; else DEST="$HOME/.claude/skills/lit-digest"; fi

if [ ! -f "$SRC/SKILL.md" ]; then echo "install.sh must stay inside its litvault/ folder (skills/lit-digest not found next to it)." >&2; exit 1; fi
if ! command -v python3 >/dev/null 2>&1; then echo "LitVault needs Python 3.9 or newer (python3 not found)." >&2; exit 1; fi

mkdir -p "$(dirname "$DEST")"
if [ -e "$DEST" ]; then
  # keep the backup outside skills/, or Claude Code would load it as a second copy of the skill
  BACKUP="$(dirname "$(dirname "$DEST")")/lit-digest.backup-$(date +%Y%m%d-%H%M%S)"
  mv "$DEST" "$BACKUP"
  echo "Moved your previous copy to $BACKUP"
fi
cp -R "$SRC" "$DEST"
find "$DEST" -name '__pycache__' -type d -prune -exec rm -rf {} +
echo "Installed LitVault to $DEST"
echo "Start Claude Code and say: start LitVault"
