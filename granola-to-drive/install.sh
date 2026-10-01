#!/bin/bash
# install.sh — set up granola-to-drive on macOS.
# Idempotent: re-running won't break anything.

set -euo pipefail

if [ -f "$HOME/.granola-to-drive.env" ]; then
  set -a
  source "$HOME/.granola-to-drive.env"
  set +a
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/Library/Application Support/Granola/claude_sync"
LAUNCHAGENT_DIR="$HOME/Library/LaunchAgents"
LAUNCHAGENT_PLIST="$LAUNCHAGENT_DIR/com.cowork.granola-fetch.plist"
LOG_DIR="$HOME/Library/Logs"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

step()    { echo -e "\n${GREEN}==>${NC} $*"; }
warn()    { echo -e "${YELLOW}!${NC} $*"; }
fatal()   { echo -e "${RED}FATAL:${NC} $*" >&2; exit 1; }

# --- 1. Sanity checks --------------------------------------------------------

step "Checking prerequisites"

[[ "$(uname)" == "Darwin" ]] || fatal "macOS only."

if [ ! -d "$HOME/Library/Application Support/Granola" ]; then
  fatal "Granola desktop app not detected at ~/Library/Application Support/Granola.
        Install it from https://granola.ai and sign in first."
fi

command -v python3 >/dev/null 2>&1 || fatal "python3 not found. Install Python 3.10+."

# --- 2. Install rclone -------------------------------------------------------

if ! command -v rclone >/dev/null 2>&1; then
  step "Installing rclone via Homebrew"
  command -v brew >/dev/null 2>&1 || fatal "Homebrew not found. Install from https://brew.sh first."
  brew install rclone
else
  echo "  rclone already installed: $(rclone --version | head -n1)"
fi

# --- 3. Configure Google Drive remote (interactive) --------------------------

REMOTE="${GRANOLA_RCLONE_REMOTE:-gdrive}"
if rclone listremotes | grep -q "^${REMOTE}:$"; then
  echo "  rclone remote '${REMOTE}' already configured. Skipping OAuth."
else
  step "Configuring rclone Google Drive remote ('${REMOTE}')"
  echo "  This will open your browser for one-time Google OAuth."
  echo "  Press Enter to start, or Ctrl-C to abort."
  read -r
  rclone config create "$REMOTE" drive scope=drive
fi

# --- 4. Copy scripts into place ----------------------------------------------

step "Copying scripts to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
BACKUP_DIR="$INSTALL_DIR/backups/$(date +%Y%m%d-%H%M%S)"
for f in granola_auth.py fetch_granola.py build_docs.py tag_meetings.py refresh.sh; do
  if [ -f "$INSTALL_DIR/$f" ] && ! cmp -s "$REPO_DIR/$f" "$INSTALL_DIR/$f"; then
    mkdir -p "$BACKUP_DIR"
    cp -p "$INSTALL_DIR/$f" "$BACKUP_DIR/$f"
  fi
  cp -v "$REPO_DIR/$f" "$INSTALL_DIR/$f"
done
chmod +x "$INSTALL_DIR/refresh.sh"

if ! python3 "$INSTALL_DIR/granola_auth.py" status; then
  python3 "$INSTALL_DIR/granola_auth.py" login
  python3 "$INSTALL_DIR/granola_auth.py" complete
fi

# --- 5. Install LaunchAgent --------------------------------------------------

step "Installing LaunchAgent"
mkdir -p "$LAUNCHAGENT_DIR" "$LOG_DIR"
sed "s|__HOME__|$HOME|g" "$REPO_DIR/com.cowork.granola-fetch.plist.template" > "$LAUNCHAGENT_PLIST"

# Reload (idempotent)
launchctl bootout "gui/$UID/com.cowork.granola-fetch" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$LAUNCHAGENT_PLIST"

echo "  Installed at $LAUNCHAGENT_PLIST"
echo "  Schedule: every hour from 7am to 9pm local time"

# --- 6. First run ------------------------------------------------------------

step "Running first sync now (this will take ~30s for the fetch + a couple minutes for upload)"
launchctl kickstart -k "gui/$UID/com.cowork.granola-fetch"
sleep 5

echo
echo "Tail the log to watch progress:"
echo "  tail -f $LOG_DIR/granola-fetch.log"
echo
echo -e "${GREEN}Done.${NC} Your Drive folder '${GRANOLA_DRIVE_FOLDER:-Knowledge Based}' will populate shortly."
echo "If you want to change the schedule, edit $LAUNCHAGENT_PLIST."
