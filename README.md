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
WATCH_FOLDER=/watch
STATE_FILE=/data/state.json
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

### 3. Start the Service

```bash
# Create directories for your files and state
mkdir -p watch data

# Start with Docker Compose
docker-compose up -d

# View logs
docker-compose logs -f
```

## Configuration

### Required Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `WATCH_FOLDER` | Path inside container to watch | `/watch` |
| `STATE_FILE` | Path inside container for state JSON | `/data/state.json` |
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

Map your local folders to container paths:

```yaml
volumes:
  - ./watch:/watch          # Your files to watch
  - ./data:/data            # State file and logs
```

## Monitoring

View service logs:

```bash
docker-compose logs -f file-watcher
```

Check state file:

```bash
cat data/state.json
```

## Troubleshooting

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
export DISCORD_BOT_TOKEN=your_token
export DISCORD_CHANNEL_ID=your_channel_id
export CHECK_INTERVAL=30s

# Run
python -m src.main
```

## License

MIT
