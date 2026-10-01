#!/bin/bash
# Refresh local Granola export: pull all meetings/transcripts/panels via Granola's
# internal API (using the sync's independently renewed login), then build per-meeting
# dossiers to upload to Drive.
#
# Runs hourly from a macOS LaunchAgent. Logs to ~/Library/Logs/granola-fetch.log.

set -uo pipefail

if [ -f "$HOME/.granola-to-drive.env" ]; then
  set -a
  source "$HOME/.granola-to-drive.env" || exit 1
  set +a
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="${GRANOLA_LOG_FILE:-$HOME/Library/Logs/granola-fetch.log}"
mkdir -p "$(dirname "$LOG")"
umask 077

# Find a working python3 — prefer Homebrew, fall back to system, then PATH lookup.
if [ -n "${GRANOLA_PYTHON:-}" ] && command -v "$GRANOLA_PYTHON" >/dev/null 2>&1; then
  PYTHON_BIN="$GRANOLA_PYTHON"
elif [ -x /opt/homebrew/bin/python3 ]; then
  PYTHON_BIN=/opt/homebrew/bin/python3
elif [ -x /usr/local/bin/python3 ]; then
  PYTHON_BIN=/usr/local/bin/python3
elif [ -x /usr/bin/python3 ]; then
  PYTHON_BIN=/usr/bin/python3
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
else
  echo "FATAL: no python3 on PATH" >> "$HOME/Library/Logs/granola-fetch.log"
  exit 127
fi

mkdir -p "$(dirname "$LOG")"

{
  echo
  echo "=========================================================="
  echo "Granola refresh starting at $(date)"
  echo "=========================================================="
} >> "$LOG" 2>&1

cd "$DIR" || { echo "FATAL: cannot cd to $DIR" >> "$LOG"; exit 2; }

# Step 1: fetch
"$PYTHON_BIN" -u fetch_granola.py >> "$LOG" 2>&1
fetch_rc=$?

if [ "$fetch_rc" -ne 0 ]; then
  echo "fetch_granola.py exited with code $fetch_rc — aborting before build." >> "$LOG"
  exit "$fetch_rc"
fi

# Step 2: build
"$PYTHON_BIN" -u build_docs.py >> "$LOG" 2>&1
build_rc=$?

if [ "$build_rc" -ne 0 ]; then
  echo "build_docs.py exited with code $build_rc." >> "$LOG"
  exit "$build_rc"
fi

# Step 2b: tag dossiers (writes a "Tags:" line into each)
"$PYTHON_BIN" -u tag_meetings.py >> "$LOG" 2>&1
tag_rc=$?
if [ "$tag_rc" -ne 0 ]; then
  echo "tag_meetings.py exited with code $tag_rc." >> "$LOG"
  # Non-fatal — keep going so Drive sync still runs.
fi

# Step 3: sync built dossiers to Drive via rclone (uploads only new/changed files)
BUILT_DIR="${GRANOLA_EXPORT_DIR:-$HOME/Library/Application Support/Granola/granola_export}/built"
RCLONE_BIN="${RCLONE_BIN:-/opt/homebrew/bin/rclone}"
[ -x "$RCLONE_BIN" ] || RCLONE_BIN="$(command -v rclone)" || RCLONE_BIN=""

if [ -z "$RCLONE_BIN" ]; then
  echo "ERROR: rclone not found on PATH — Drive upload cannot run." >> "$LOG"
  exit 127
else
  echo "--- rclone copy starting at $(date) ---" >> "$LOG"
  "$RCLONE_BIN" copy "$BUILT_DIR" "${GRANOLA_RCLONE_REMOTE:-gdrive}:${GRANOLA_DRIVE_FOLDER:-Knowledge Based}/" \
      --include "*.txt" --update --transfers 8 --checkers 8 \
      --log-file "$LOG" --log-level INFO
  rclone_rc=$?
  if [ "$rclone_rc" -ne 0 ]; then
    echo "rclone exited with code $rclone_rc." >> "$LOG"
    exit "$rclone_rc"
  fi
fi

echo "Refresh OK at $(date)" >> "$LOG"
