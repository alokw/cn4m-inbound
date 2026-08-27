# Inbound

Lightweight Docker service that watches a folder and pings your Discord channel through a webhook when new files arrive.

## Features

- 🔍 **Recursive folder scanning** - Monitors all subdirectories
- 📁 **File tracking** - Maintains persistent state in JSON file
- 🎯 **Smart growth detection** - Waits for large files to finish uploading before notifying
- 🔔 **Discord notifications** - Get notified of new, deleted, or moved files via a channel webhook
- 🌙 **Quiet hours** - Suppress notifications during sleep time, send summary in the morning
- 📊 **Rich metadata** - Tracks file size, MIME type, modification time, and folder structure
- 🐳 **Docker ready** - Simple deployment with Docker Compose
- ⚙️ **Configurable** - All settings via environment variables

## Quick Start

### 1. Create a Discord Webhook

1. In Discord, open the target channel's settings (or **Server Settings → Integrations**)
2. Go to **Integrations → Webhooks** and click **New Webhook**
3. Give it a name and confirm the channel it posts to
4. Click **Copy Webhook URL**

No bot, no application, and no gateway connection is required — the service just POSTs to that URL. Treat the webhook URL like a password: anyone who has it can post to your channel.

### 2. Configure Environment Variables

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

Edit `.env` with your settings:

```env
# Required
WATCH_FOLDER=/path/to/folder/to/watch
STATE_FILE=/path/to/state.json
LOG_FILE=/path/to/watcher.log
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/your_webhook_id/your_webhook_token
CHECK_INTERVAL=5m

# Optional (with defaults shown)
STABILITY_CHECKS=3
QUIET_HOURS_ENABLED=false
QUIET_HOURS_START=22
QUIET_HOURS_END=8
TIMEZONE=UTC
MIN_FILE_SIZE=0
```

`WATCH_FOLDER`, `STATE_FILE`, and `LOG_FILE` are **host paths** that get bind-mounted into the container. The state and log files must exist on the host before starting — Docker will create a directory instead of a file if they don't:

```bash
# Linux/macOS
touch /path/to/state.json /path/to/watcher.log

# Windows (PowerShell)
New-Item -ItemType File -Force "/path/to/state.json"
New-Item -ItemType File -Force "/path/to/watcher.log"
```

### 3. Start the Service

```bash
# Start with Docker Compose
docker-compose up -d

# View logs
docker-compose logs -f
```

## Configuration

### Required Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `WATCH_FOLDER` | Host path to the folder to watch | `/mnt/files/incoming` |
| `STATE_FILE` | Host path for the state JSON file | `/mnt/data/state.json` |
| `LOG_FILE` | Host path for the watcher log file | `/mnt/data/watcher.log` |
| `DISCORD_WEBHOOK_URL` | Discord webhook URL for notifications | `https://discord.com/api/webhooks/123.../abc...` |
| `CHECK_INTERVAL` | How often to check (with suffix) | `5m`, `30s`, `1h` |

### Optional Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `STABILITY_CHECKS` | Number of stable size checks before notification | `3` |
| `QUIET_HOURS_ENABLED` | Enable quiet hours | `false` |
| `QUIET_HOURS_START` | Hour when quiet hours start (0-23) | `22` |
| `QUIET_HOURS_END` | Hour when quiet hours end (0-23) | `8` |
| `TIMEZONE` | Timezone for quiet hours | `UTC` |
| `MIN_FILE_SIZE` | Only check files >= this size for growth (bytes) | `0` |
| `EXCLUDE_PATTERNS` | Comma-separated glob patterns to ignore (matched against full relative path and each folder/file name component) | `~private*,*.tmp` |

### Exclude Patterns

Patterns are matched against both the full relative path and each individual folder/file name component. This means folder-based patterns like `System Volume Information` will automatically exclude everything inside that folder without needing a trailing `*`.

**Do not add a trailing `/*` to a folder pattern.** A component is never matched against a pattern containing `/`, and the full-path match is anchored at the start, so `_ARCHIVE/*` only excludes an `_ARCHIVE` folder at the top level and silently misses `1400/_ARCHIVE/`. Use the bare folder name instead:

```env
# Excludes every _ARCHIVE folder at any depth
EXCLUDE_PATTERNS=_ARCHIVE
```

Use `*_ARCHIVE*` only if you also want to match folders like `OLD_ARCHIVE` — note it will match *files* with `_ARCHIVE` in the name too.

Spaces within pattern names are fine — values are split on commas only, not spaces. No quoting is needed.

Supported glob characters:

| Character | Meaning |
|-----------|---------|
| `*` | Matches anything (including nothing) |
| `?` | Matches any single character |
| `[seq]` | Matches any character in `seq` |

**Matching a literal `$`:** Docker Compose interpolates `$NAME` in `.env` as a variable, so a pattern like `$Recycle.Bin` silently becomes `.Bin` (Compose also logs `The "Recycle" variable is not set`). Escape the dollar sign by doubling it:

```env
# Excludes the Windows Recycle Bin folder
EXCLUDE_PATTERNS=$$Recycle.Bin
```

**Matching literal brackets:** Square brackets are special in glob syntax. To match a filename that literally contains `[` or `]` (e.g. `[Auto Save]`), escape them as `[[]` and `[]]`:

```env
# Matches files containing [Auto Save] in the name
EXCLUDE_PATTERNS=*[[]Auto Save[]]*
```

A typical set of patterns for Windows watch folders:

```env
EXCLUDE_PATTERNS=System Volume Information,$$Recycle.Bin,RECYCLE?,Recovery,thumbs.db,*.DS_*,*[[]Auto Save[]]*,*.tmp
```

### Check Interval Format

The `CHECK_INTERVAL` uses the format `<number><unit>`:

- `30s` - 30 seconds
- `5m` - 5 minutes
- `1h` - 1 hour
- `1d` - 1 day

## How It Works

### File Growth Detection

For large files (e.g., 100+ GB uploads that take hours), the service:

1. Detects new files on each scan
2. Tracks file size across multiple checks
3. Only sends notification when file size is stable (unchanged for N checks)
4. Configurable via `STABILITY_CHECKS` (default: 3)

This prevents notifications for files that are still uploading.

### Quiet Hours

When quiet hours are enabled:

- Service skips folder checks during quiet hours
- Files added during quiet hours are discovered when quiet hours end
- Single summary notification sent with all discovered files
- Files that start uploading during quiet hours and finish after are treated as new files

Example setup (sleep from 10pm to 8am):

```env
QUIET_HOURS_ENABLED=true
QUIET_HOURS_START=22
QUIET_HOURS_END=8
TIMEZONE=America/New_York
```

### State Tracking

The service maintains a JSON state file (`state.json`) that tracks:

- All files ever discovered
- File metadata (size, type, modification time)
- Pending files still growing
- Last check timestamp
- Quiet hours status

This ensures:

- No duplicate notifications
- Notifications persist across container restarts
- Detection of deleted or moved files

### Folder Structure

The service preserves full folder paths in notifications:

```
📁 New file(s) discovered:
• uploads/2026/march/video.mp4 (50.2 GB, video/mp4)
• images/photo.jpg (2.4 MB, image/jpeg)
```

## Docker Volume Mapping

`WATCH_FOLDER`, `STATE_FILE`, and `LOG_FILE` in `.env` are host paths that get bind-mounted into the container automatically. There is no need to edit `docker-compose.yml` for different instances — just point each `.env` to different host paths.

Running multiple watches against a shared data folder is straightforward:

```env
# Instance 1 .env
WATCH_FOLDER=/mnt/incoming/show_a
STATE_FILE=/mnt/data/show_a_state.json
LOG_FILE=/mnt/data/show_a.log

# Instance 2 .env
WATCH_FOLDER=/mnt/incoming/show_b
STATE_FILE=/mnt/data/show_b_state.json
LOG_FILE=/mnt/data/show_b.log
```

## Monitoring

View service logs:

```bash
docker-compose logs -f file-watcher
```

Check state file and logs directly at the host paths defined in your `.env`:

```bash
cat /path/to/state.json
tail -f /path/to/watcher.log
```

## Troubleshooting

### Build warning: git was not found in the system

During `docker-compose up --build` you may see:

```
level=warning msg="current commit information was not captured by the build"
```

This is Docker BuildKit trying to embed the current git commit SHA into the image metadata. It's harmless and has no effect on the container. To suppress it, set the following environment variable before building:

```bash
# Linux/macOS
export BUILDX_NO_DEFAULT_PROVENANCE=1

# Windows (PowerShell)
$env:BUILDX_NO_DEFAULT_PROVENANCE=1
```

### Resetting state to redetect all files

The state is loaded into memory at startup and written back to disk after each cycle. Clearing the state file while the container is running has no effect — it will be overwritten on the next cycle.

To force all files to be treated as new:

```bash
# Windows (PowerShell)
Clear-Content "M:\inbound_data\your_state.json"
docker-compose restart

# Linux/macOS
echo '' > /path/to/state.json
docker-compose restart
```

### Webhook not sending messages

1. Verify `DISCORD_WEBHOOK_URL` is complete and unquoted (it must include both the webhook ID and token)
2. Check the webhook still exists in **Channel Settings → Integrations → Webhooks** — deleting it returns `404 Unknown Webhook`
3. Check the logs for the HTTP status the service reports (`401`/`404` means a bad or deleted webhook, `429` means rate limiting)
4. Confirm the container has outbound network access to `discord.com`

### Files not being detected

1. Check volume mappings are correct
2. Verify files are in the watch folder
3. Check container has read permissions
4. Review logs for errors

### Large files triggering too early

Increase `STABILITY_CHECKS`:

```env
STABILITY_CHECKS=5
```

### Notifications during sleep

Enable quiet hours:

```env
QUIET_HOURS_ENABLED=true
QUIET_HOURS_START=22
QUIET_HOURS_END=8
TIMEZONE=Your_Timezone
```

## Development

Run locally without Docker:

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export WATCH_FOLDER=./watch
export STATE_FILE=./data/state.json
export LOG_FILE=./data/watcher.log
export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/your_webhook_id/your_webhook_token
export CHECK_INTERVAL=30s

# Run
python -m src.main
```

## License

MIT
