# Inbound

Lightweight Docker service that watches a folder and pings your Discord channel through a webhook when new files arrive.

## Features

- 🔍 **Recursive folder scanning** - Monitors all subdirectories
- 📁 **File tracking** - Maintains persistent state in JSON file
- 🎯 **Smart growth detection** - Waits for large files to finish uploading before notifying
- 🔔 **Discord notifications** - Get notified of new, deleted, or moved files via a channel webhook
- 🌙 **Quiet hours** - Suppress notifications during sleep time, send summary in the morning
- 📊 **Rich metadata** - Tracks file size, MIME type, modification time, and folder structure
- 📟 **Status page** - Settings, live status, and an activity log on port 2645
- 📡 **cn4m updates** - One-line status pushes to the parent system when a scan finds something
- 🔗 **symmetry triggers** - Tells symmetry the moment a file is complete, so it links it now rather than on its next scan
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
| `WEB_UI_ENABLED` | Serve the status page | `true` |
| `WEB_UI_PORT` | Port for the status page | `2645` |
| `WEB_UI_HOST` | Interface the status page binds | `0.0.0.0` |
| `STATUS_URL` | cn4m status endpoint (empty disables updates) | `http://host.docker.internal:2640/suite/status` in Docker, `localhost` otherwise |
| `STATUS_APP_NAME` | Name this service reports itself as to cn4m | `inbound` |
| `SYMMETRY_RESCAN_URL` | symmetry rescan endpoint (empty disables triggers) | `http://host.docker.internal:2647/api/rescan` in Docker, `localhost` otherwise |
| `SYMMETRY_RESCAN_TOKEN` | symmetry's `WEBHOOK_TOKEN`, if it has one | none |

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

A file is therefore announced after one scan to discover it plus
`STABILITY_CHECKS` scans where its size did not move — so the delay between an
upload finishing and the notification is about
`(STABILITY_CHECKS + 1) x CHECK_INTERVAL`. With the defaults (`3` and `5m`)
that is roughly 20 minutes. Any growth at any point resets the count.

Files still being counted are listed on the [status page](#status-page) with
their progress (`2 of 3 stable checks`), so a file that seems stuck is easy to
tell apart from one that was never seen.

### Pending entries are reconciled every scan

A file leaves the pending list when it settles or when it is seen to be deleted.
Two cases fit neither, so each scan clears them explicitly:

- **Already stable at the same size** — bookkeeping left behind rather than a
  file that is still growing
- **Renamed or removed before settling** — delete detection never sees these,
  because it only looks at files that made it to the tracked list. Renaming a
  file mid-upload (`v003` to `v004`, adding ` - Copy`) is the usual way to
  create one

A tracked file that is *being modified* is legitimately in both lists at once,
so an entry only counts as stale when its pending size matches the tracked size.
An in-flight change keeps its place in the queue and is announced when it
settles.

If a scan comes back empty while files are still tracked, the watch folder is
unreadable rather than empty — the cleanup is skipped and a warning logged, so a
dropped network mount cannot flush the state.

Cleanups appear in the activity log, so a large one-off clear is visible rather
than silent.

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

## Status page

The service serves a small read-only page on **port 2645**:

```
http://localhost:2645
```

It shows three things, refreshed every five seconds:

- **Totals** — scans run, files discovered and removed, Discord submissions sent
  and failed, cn4m updates sent and failed, and errors
- **Live status and settings** — every setting the service is actually running
  with, plus the last check time, next check countdown, quiet-hours state, file
  and byte counts, and what was last said to cn4m
- **Activity log** — recent successes, failures, and Discord submissions,
  filterable by source (`scan`, `discord`, `cn4m`, `service`) and level

The log lives in memory (the last 500 events) and is rebuilt from scratch on
restart — `LOG_FILE` remains the durable record. Warnings and errors from
anywhere in the service are mirrored into it automatically, so a failure shows
up on the page whether or not the code that raised it knew about the page.

Nothing on the page can change the running service, and the `DISCORD_WEBHOOK_URL`
token is masked. There is no authentication, so bind it to a trusted network:

```env
WEB_UI_HOST=127.0.0.1
```

Two other endpoints are available for scripting:

| Endpoint | Returns |
|----------|---------|
| `/api/status` | Settings, live status, and counters as JSON |
| `/api/events` | Recent events as JSON (`?limit=`, `?category=`, `?level=`) |
| `/healthz` | `ok`, for container health checks |

Set `WEB_UI_ENABLED=false` to turn it off. If the port is already taken, the
service logs the error and keeps watching — the page is never load-bearing.

### Port mapping

In Docker the container always listens on 2645; `WEB_UI_PORT` in `.env` chooses
the **host** port it maps to. Running several instances side by side:

```env
# Instance 1 .env
WEB_UI_PORT=2645

# Instance 2 .env
WEB_UI_PORT=2646
```

## Telling cn4m what happened

Set `STATUS_URL` and every scan that finds something posts a one-line update:

```
POST http://<cn4m-host>:2640/suite/status
app=inbound&message=Discovered+5+new+assets&level=ok
```

Only scans that actually found something send anything — a quiet scan stays
quiet, so the endpoint sees traffic when there is news rather than once a minute
forever. Files are reported when they are *stable*, not when they first appear,
so a large upload produces one update when it lands rather than one per scan
while it grows. If known files also changed or disappeared, the message says so:
`Discovered 5 new assets, refreshed 1, removed 2 files`.

### localhost means the container

**Inside Docker, `localhost` is the container itself**, not the machine running
Docker, so `http://localhost:2640/...` from a containerised inbound reaches
nothing. Left unset, `STATUS_URL` (and `SYMMETRY_RESCAN_URL` below) picks the
host that works for wherever it finds itself running: `host.docker.internal`
inside a container, `localhost` outside one. Set it explicitly when neither
fits:

| Where the other tool runs | Host to use |
| --- | --- |
| On the Docker host, or in its own container with a published port | `host.docker.internal` |
| As a container on a network shared with inbound | `<its service name>` |
| Same machine, no Docker | `localhost` |

Two containers from *separate* `docker-compose.yml` files are on separate
networks by default and cannot see each other's service names — which is why
the middle row rarely applies in this suite, and `host.docker.internal` against
the published port is the one that works. `docker-compose.yml` maps that name
to the host gateway, so it works on Linux as well as Docker Desktop. If a URL
fails for a networking reason the log says which one, rather than just
reporting a refused connection.

### When cn4m is not there

A failing endpoint is left alone rather than retried every scan: after a failure
updates pause for 60 seconds, then 2, 4, 8 minutes and so on up to 30, and the
first success resets it. So an unset, wrong, or temporarily down cn4m costs one
attempt and a single log line, not a broken request every scan. An HTTP error
such as a 404 backs off the same way a refused connection does.

Updates go out as detached tasks and swallow their errors, so a cn4m that is
slow, down, or not there at all cannot delay or interrupt watching. A failure is
logged once at `WARNING`, then at `DEBUG` until it recovers. Setting `STATUS_URL`
empty disables the whole thing. In-flight updates get a moment to finish on
shutdown, which is what makes the update from the last scan before
`docker stop` actually arrive.

The current backoff state and the last message sent are both shown on the status
page, so you can tell at a glance whether cn4m is hearing from this service.

## Telling symmetry a file is complete

symmetry mirrors the same folder this service watches, creating links for the
repository. On its own it waits for each file to settle before linking it. This
service already knows the moment a file has settled, so with
`SYMMETRY_RESCAN_URL` set it vouches for the file and symmetry links it on
sight instead of waiting out its own settle period:

```
POST http://<symmetry-host>:2647/api/rescan
path=1100/new_asset.mov&path=1100/other_asset.mov
```

One request per scan carries every file that went stable in it, so ten files
landing together cost one round trip, not ten. Paths are relative to the watch
folder with forward slashes — that is what symmetry keys on (relative to its
`SOURCE_DIR`, as POSIX), so **the two tools must be rooted at the same folder**:
inbound's `WATCH_FOLDER` and symmetry's `SOURCE_DIR` have to be the same tree,
even though one is a host path and the other a container path.

Files that were already known and changed are vouched for too, not just new
ones — a replaced file is a file symmetry needs to relink.

If symmetry has `WEBHOOK_TOKEN` set, put the same value in
`SYMMETRY_RESCAN_TOKEN`; it travels as an `X-Webhook-Token` header. A wrong
token gets a `401`, which backs off like any other failure and is shown as the
last result on the status page.

Everything said above about cn4m being absent applies here unchanged: symmetry
does not have to be running. A missing symmetry costs one attempt and a log
line, then backs off from 60 seconds up to 30 minutes, and never delays a scan.
The trigger goes out *before* the Discord notification, so a slow Discord
cannot hold it up. Setting `SYMMETRY_RESCAN_URL` empty disables it.

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

The quickest look is the [status page](#status-page) on port 2645.

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

### Files stuck as "still growing"

The status page counting files that finished uploading long ago means pending
entries were left behind. Every scan now clears both causes automatically, so
one check cycle after upgrading the count corrects itself — no state reset is
needed, and the tracked files are not touched. The activity log records what was
cleared.

If a file is genuinely stuck at `0 of N stable checks`, its size is changing on
every scan: either it is still being written, or something is touching it
repeatedly. Compare its size across two entries in `LOG_FILE` to tell which.

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

### Status page not reachable

1. Confirm `WEB_UI_ENABLED` is not `false`
2. Check the startup logs for `Web UI listening on ...` — if the port was already
   taken the service logs the error and carries on watching without the page
3. In Docker, check the host port mapping (`docker-compose ps`); the container
   always listens on 2645 regardless of `WEB_UI_PORT`
4. If `WEB_UI_HOST` is `127.0.0.1` the page is unreachable from outside the
   container — use `0.0.0.0` and rely on the port mapping to limit exposure

### cn4m is not receiving updates

1. Look at **cn4m last result** and **cn4m backoff** on the status page
2. A quiet scan sends nothing by design — updates only go out when a scan finds,
   refreshes, or loses files
3. Inside Docker, `localhost` is the container; see
   [localhost means the container](#localhost-means-the-container)
4. After a failure updates pause and the gap doubles, so a fix may take up to
   30 minutes to be retried — restart the container to retry immediately

### symmetry is not linking files any faster

1. Look at **symmetry last request** and **symmetry last result** on the status
   page — `ok` means symmetry accepted the request
2. `401` means symmetry has a `WEBHOOK_TOKEN` and `SYMMETRY_RESCAN_TOKEN` does
   not match it
3. If the request is accepted but the file still waits, the path does not match
   what symmetry sees: check that `WATCH_FOLDER` here and `SOURCE_DIR` there are
   the same tree. symmetry logs `rescan requested (N paths vouched for)` on its
   side when a request arrives
4. Same networking rules as cn4m: from inside Docker, `localhost` is the
   container; see [localhost means the container](#localhost-means-the-container)

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
export STATUS_URL=http://localhost:2640/suite/status
export SYMMETRY_RESCAN_URL=http://localhost:2647/api/rescan

# Run
python -m src.main
```

The status page is then at <http://localhost:2645>.

```bash
# Watch what it is reporting without opening a browser
curl -s localhost:2645/api/status | jq .live
curl -s "localhost:2645/api/events?level=error" | jq -r '.events[].message'
```

## License

MIT
