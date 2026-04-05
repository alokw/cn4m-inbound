# Inbound

Lightweight Docker service that watches a folder and pings your Discord when new files arrive.

## Features

- 🔍 **Recursive folder scanning** - Monitors all subdirectories
- 📁 **File tracking** - Maintains persistent state in JSON file
- 🎯 **Smart growth detection** - Waits for large files to finish uploading before notifying
- 🔔 **Discord notifications** - Get notified of new, deleted, or moved files
- 🌙 **Quiet hours** - Suppress notifications during sleep time, send summary in the morning
- 📊 **Rich metadata** - Tracks file size, MIME type, modification time, and folder structure
- 🐳 **Docker ready** - Simple deployment with Docker Compose
- ⚙️ **Configurable** - All settings via environment variables

## Quick Start

### 1. Create a Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Create a new application
3. Go to the "Bot" section and create a bot
4. Enable "MESSAGE CONTENT INTENT" (if required)
5. Copy the bot token
6. Get your Channel ID (enable Developer Mode in Discord, right-click channel)

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
DISCORD_BOT_TOKEN=your_bot_token_here
DISCORD_CHANNEL_ID=your_channel_id_here
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
| `DISCORD_BOT_TOKEN` | Your Discord bot token | `MTIzNDU2Nzg5...` |
| `DISCORD_CHANNEL_ID` | Discord channel ID for notifications | `123456789012345678` |
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

Spaces within pattern names are fine — values are split on commas only, not spaces. No quoting is needed.

Supported glob characters:

| Character | Meaning |
|-----------|---------|
| `*` | Matches anything (including nothing) |
| `?` | Matches any single character |
| `[seq]` | Matches any character in `seq` |

**Matching literal brackets:** Square brackets are special in glob syntax. To match a filename that literally contains `[` or `]` (e.g. `[Auto Save]`), escape them as `[[]` and `[]]`:

```env
# Matches files containing [Auto Save] in the name
EXCLUDE_PATTERNS=*[[]Auto Save[]]*
```

A typical set of patterns for Windows watch folders:

```env
EXCLUDE_PATTERNS=System Volume Information,$Recycle.Bin,RECYCLE?,Recovery,thumbs.db,*.DS_*,*[[]Auto Save[]]*,*.tmp
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

### Bot not sending messages

1. Verify bot token is correct
2. Check bot has permission to send messages in the channel
3. Check bot is invited to the server
4. Verify `DISCORD_CHANNEL_ID` is correct

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
export DISCORD_BOT_TOKEN=your_token
export DISCORD_CHANNEL_ID=your_channel_id
export CHECK_INTERVAL=30s

# Run
python -m src.main
```

## License

MIT
