# granola-to-drive

Auto-sync your [Granola](https://granola.ai) meeting notes — including **full transcripts** — to a Google Drive folder so you can search them, share them, or hand them to an AI assistant.

Runs autonomously every hour on your Mac. No paid Granola plan required.

## What you get

For every meeting, a plain-text dossier in your Drive folder:

```
Khanh Dang
Date: Apr 22, 2026 7:46 PM
Granola Meeting ID: b6b44320-8e21-4870-8db8-dcc02585aab4
Tags: investor-pitch | aerospace-defense, fundraising, humanoids
Participants: Dillon Johnson <…>, Khanh Dang <…>

================================================================
AI SUMMARY
================================================================
…the structured AI summary panel from Granola…

================================================================
PRIVATE NOTES
================================================================
…your own typed notes during the meeting…

================================================================
FULL TRANSCRIPT
================================================================
[00:00:03] Host: …
[00:00:11] You: …
…
```

Filenames are `<title>, YYYY-MMM-DD.txt` — readable in Finder and Drive.

## How it works

The export scripts run in sequence, kicked off by a macOS LaunchAgent every hour:

1. **`fetch_granola.py`** uses a separate Granola sign-in managed by **`granola_auth.py`**, then calls Granola's internal API to pull every meeting's metadata, AI summary panels, and full transcript. The sync renews its own credentials automatically. Saves to `~/Library/Application Support/Granola/granola_export/`.
2. **`build_docs.py`** turns the raw JSON into one plain-text dossier per meeting.
3. **`tag_meetings.py`** (optional) injects a `Tags:` line into each dossier using simple keyword rules — `customer-discovery`, `investor-pitch`, etc. Edit the rules to match your own buckets.
4. **`rclone copy`** uploads only new/changed dossiers to a folder in your Drive.

Meeting access is read-only. The sync creates and renews its own authentication session; it never modifies meeting data or the desktop app's credentials. Current Granola versions encrypt desktop storage with an app-protected Keychain key, so the old `supabase.json` approach no longer works.

## Setup (3 minutes)

### Prerequisites
- macOS with Granola desktop app installed and signed in
- Homebrew
- Python 3.10+

### One-command install
```bash
git clone https://github.com/<you>/granola-to-drive.git ~/granola-to-drive
cd ~/granola-to-drive
./install.sh
```

The installer will:
1. `brew install rclone`
2. Run `rclone config` to authenticate with your Google account (one-time browser OAuth)
3. Copy the scripts into `~/Library/Application Support/Granola/claude_sync/`
4. Connect a separate Granola session through your browser (one time)
5. Install the LaunchAgent at `~/Library/LaunchAgents/com.cowork.granola-fetch.plist`
6. Kick off a manual test run to backfill all your meetings

By default the LaunchAgent fires every hour from 7am–9pm. Edit the plist if you want a different schedule.

### Repairing an older installation

Run `./install.sh` again from this updated checkout. When prompted:

1. Open the printed Granola sign-in URL and sign in to your existing account.
2. Cancel the browser's prompt to open Granola. Copy the browser address from the “Opening Granola” page.
3. Paste that address at the terminal's hidden prompt.

The sync stores its separate credentials at `~/Library/Application Support/granola-to-drive/auth.json` with owner-only permissions. It renews them automatically without requiring the desktop app to be open. Never commit or share this file. The Google login requests basic sign-in information, without requesting additional calendar permissions.

To reconnect later:

```bash
cd ~/Library/Application\ Support/Granola/claude_sync
python3 granola_auth.py login
python3 granola_auth.py complete
```

The `login` command prints a URL; `complete` prompts for the resulting browser address. Use `login --provider microsoft` for Microsoft accounts. Existing unencrypted desktop credentials remain a read-only fallback for older Granola installations; expired desktop refresh tokens are never reused.

### Configuration

Set these env vars in `~/.granola-to-drive.env` (or just edit `refresh.sh` directly):

```bash
# Drive folder name (will be created if missing). Default: "Knowledge Based"
export GRANOLA_DRIVE_FOLDER="Knowledge Based"

# rclone remote name (created during `rclone config`). Default: "gdrive"
export GRANOLA_RCLONE_REMOTE="gdrive"

# Optional: use a different local export directory for all export stages
# export GRANOLA_EXPORT_DIR="$HOME/Documents/granola_export"
```

## Using the synced data

### Search in Drive
- Filter by tag: search `Tags: customer-discovery` or `aerospace-defense`
- Find a person: search the participant's email or name
- Find a topic: full-text search picks up summary + private notes + full transcript

### Ask Claude / ChatGPT / etc.
With Drive connected to Cowork (or any LLM that can read Drive), ask things like:
> *"Summarize what Roger Misiaszek said about explosives manufacturing automation requirements."*
> *"Find every meeting where someone mentioned clean rooms."*
> *"What were the top three customer pain points in my mentor-advisor sessions about fundraising?"*

The model just reads the dossier files.

## Customizing the tags

Edit `tag_meetings.py`:

- **`TITLE_RULES`** — regex matchers against meeting titles. First match wins.
- **`OVERRIDES`** — keyed by Granola meeting UUID. Use this for one-off corrections after reviewing the auto-tags.
- **`INDUSTRY_KEYWORDS`** / **`TOPIC_KEYWORDS`** — body keyword matchers that add secondary tags.

After editing, just run:
```bash
python3 ~/Library/Application\ Support/Granola/claude_sync/tag_meetings.py
```
…and the next hourly sync will push the updates to Drive.

## Troubleshooting

**"Credentials not found", "Token expired", or encrypted desktop credentials in the log**
Update the installation and connect the separate sync login as described above. Opening Granola no longer updates the old plaintext credentials. If the sync session is revoked, run `granola_auth.py login` and `granola_auth.py complete` again. Network and authentication failures stop the run with an error; they do not report a successful upload.

**Nothing showing up in Drive**
Check `~/Library/Logs/granola-fetch.log` for errors. The script logs every step.

**Granola changes their API**
This uses an undocumented internal API. If Granola changes endpoints or request shape, fix `fetch_granola.py`. The client version is read from the installed Granola app. Check the references below if the API changes again.

## Caveats

- **Unofficial** — uses Granola's internal API. Could break with any Granola update.
- **Read-only meeting access** — creates and renews an authentication session, but never edits meetings. Personal use only.
- **Your local sync `auth.json` can read your Granola data.** Treat the file like a password. Rotated refresh tokens are saved atomically under a file lock.
- **Single-user** — designed for one Mac, one Drive account. Multi-user not supported.

## Verification

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh refresh.sh
```

Fetches preserve the previous document index on failures, retry changed meetings, and retry old empty transcript/summary responses after 24 hours. Uploads use `rclone copy`, so files already in Drive are not deleted.

Run verification from the repository root. The nested `granola-to-drive/` directory is a compatibility copy of the scripts; keep it in sync with the repository root when changing them.

## License

MIT. See [LICENSE](LICENSE).

## Credits

Built by [Dillon Johnson](https://github.com/dillonjohnson). Inspired by community reverse-engineering of Granola's API:
- [josephthacker.com/hacking/2025/05/08/reverse-engineering-granola-notes.html](https://josephthacker.com/hacking/2025/05/08/reverse-engineering-granola-notes.html)
- [magarcia/granola-cli](https://github.com/magarcia/granola-cli)
- [getprobo/reverse-engineering-granola-api](https://github.com/getprobo/reverse-engineering-granola-api)
- [dannymcc/Granola-to-Obsidian](https://github.com/dannymcc/Granola-to-Obsidian)
