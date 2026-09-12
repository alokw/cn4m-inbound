"""Small read-only web UI showing settings, live status, and recent activity.

Served by aiohttp alongside the watcher loop, so it adds no dependencies and
no extra process. Everything it exposes is read-only - there are no controls
that can change the running service.
"""

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from aiohttp import web

from src.utils import format_size, get_env_bool, get_env_int

logger = logging.getLogger(__name__)

DEFAULT_PORT = 2645
DEFAULT_HOST = '0.0.0.0'


def redact_webhook(url: Optional[str]) -> str:
    """
    Mask the secret half of a Discord webhook URL.

    Args:
        url: Full webhook URL, or None

    Returns:
        URL with the token replaced, or 'not set'
    """
    if not url:
        return 'not set'

    match = re.match(r'^(https?://[^/]+/api/webhooks/)([^/]+)/(.+)$', url.strip())
    if not match:
        # Never echo something that might be the whole secret
        return 'set (unrecognized format)'

    prefix, webhook_id, _token = match.groups()
    visible_id = webhook_id[:4] + '…' + webhook_id[-4:] if len(webhook_id) > 8 else webhook_id
    return f"{prefix}{visible_id}/••••••"


def _humanize_seconds(seconds: float) -> str:
    """
    Format a duration as a compact human string.

    Args:
        seconds: Duration in seconds

    Returns:
        String like '3d 4h 12m' or '45s'
    """
    seconds = int(max(0, seconds))

    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")

    return ' '.join(parts)


class WebUI:
    """Serves the status page and its JSON endpoints."""

    def __init__(self, service, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        """
        Initialize the web UI.

        Args:
            service: The running FileWatcherService
            host: Interface to bind
            port: Port to listen on
        """
        self.service = service
        self.host = host
        self.port = port

        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

    async def start(self) -> None:
        """Bind the port and start serving. Failure here does not stop the watcher."""
        app = web.Application()
        app.router.add_get('/', self.handle_index)
        app.router.add_get('/api/status', self.handle_status)
        app.router.add_get('/api/events', self.handle_events)
        app.router.add_get('/healthz', self.handle_health)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()

        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()

        logger.info(f"Web UI listening on http://{self.host}:{self.port}")

    async def stop(self) -> None:
        """Stop serving and release the port."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            logger.info("Web UI stopped")

    async def handle_index(self, request: web.Request) -> web.Response:
        """
        Serve the status page.

        Args:
            request: Incoming request

        Returns:
            HTML response
        """
        return web.Response(text=PAGE_HTML, content_type='text/html')

    async def handle_health(self, request: web.Request) -> web.Response:
        """
        Serve a plain liveness check.

        Args:
            request: Incoming request

        Returns:
            Plain text response
        """
        return web.Response(text='ok')

    async def handle_status(self, request: web.Request) -> web.Response:
        """
        Serve settings and live status as JSON.

        Args:
            request: Incoming request

        Returns:
            JSON response
        """
        return web.json_response(self.build_status())

    async def handle_events(self, request: web.Request) -> web.Response:
        """
        Serve recent activity events as JSON.

        Args:
            request: Incoming request

        Returns:
            JSON response
        """
        try:
            limit = int(request.query.get('limit', 200))
        except ValueError:
            limit = 200

        category = request.query.get('category') or None
        level = request.query.get('level') or None

        events = self.service.activity_log.events(
            limit=max(1, min(limit, 500)),
            category=category,
            level=level,
        )
        return web.json_response({'events': events})

    def build_status(self) -> Dict[str, Any]:
        """
        Collect everything the page shows.

        Returns:
            Dictionary of settings, live status, and counters
        """
        service = self.service
        watcher = service.file_watcher
        state = service.state_manager
        activity = service.activity_log
        reporter = service.status_pusher
        rescan = service.rescan_trigger

        tracked = state.get_all_files() if state else {}
        pending = state.get_pending_files() if state else {}
        total_bytes = sum(f.get('size', 0) for f in tracked.values())

        last_check = state.state.get('last_check') if state else None
        next_check_in = None
        if service.last_check_wallclock is not None and service.check_interval:
            elapsed = datetime.now(timezone.utc).timestamp() - service.last_check_wallclock
            next_check_in = max(0, int(service.check_interval - elapsed))

        counters = activity.counters()

        return {
            'service': {
                'name': 'inbound',
                'started_at': activity.started_at.isoformat(),
                'uptime': _humanize_seconds(activity.uptime_seconds()),
                'running': service.running,
            },
            'settings': {
                'Watch folder': str(watcher.watch_folder) if watcher else 'unknown',
                'State file': str(state.state_file) if state else 'unknown',
                'Log file': os.getenv('LOG_FILE', '/watcher.log'),
                'Check interval': f"{service.check_interval}s" if service.check_interval else 'unknown',
                'Stability checks': watcher.stability_checks if watcher else '-',
                'Minimum file size': format_size(watcher.min_file_size) if watcher else '-',
                'Timezone': os.getenv('TIMEZONE', 'UTC'),
                'Quiet hours': (
                    f"{watcher.quiet_hours_start:02d}:00 - {watcher.quiet_hours_end:02d}:00"
                    if watcher and watcher.quiet_hours_enabled else 'disabled'
                ),
                'Exclude patterns': (
                    ', '.join(watcher.exclude_patterns) if watcher and watcher.exclude_patterns else 'none'
                ),
                'Discord webhook': redact_webhook(os.getenv('DISCORD_WEBHOOK_URL')),
                'cn4m status URL': reporter.url if reporter and reporter.enabled else 'disabled',
                'cn4m app name': reporter.app if reporter else '-',
                'symmetry rescan URL': rescan.url if rescan and rescan.enabled else 'disabled',
                'symmetry token': 'set' if rescan and rescan.token else 'none',
                'Web UI': f"{self.host}:{self.port}",
            },
            'live': {
                'Last check': last_check or 'not yet',
                'Next check in': f"{next_check_in}s" if next_check_in is not None else 'unknown',
                'Quiet hours now': 'yes' if (watcher and watcher.is_quiet_hours()) else 'no',
                'Files tracked': len(tracked),
                'Files pending (still growing)': len(pending),
                'Total size tracked': format_size(total_bytes),
                'cn4m last message': (reporter.last_message or 'none') if reporter else 'none',
                'cn4m last result': (reporter.last_result or 'not attempted') if reporter else 'disabled',
                'cn4m backoff': (reporter.describe_backoff() or 'none') if reporter else 'n/a',
                'symmetry last request': (rescan.last_message or 'none') if rescan else 'none',
                'symmetry last result': (rescan.last_result or 'not attempted') if rescan else 'disabled',
                'symmetry backoff': (rescan.describe_backoff() or 'none') if rescan else 'n/a',
            },
            'counters': counters,
            'pending_files': [
                {'path': path,
                 'size': format_size(info.get('size', 0)),
                 'stable_checks': info.get('stable_checks', 0),
                 'required_checks': watcher.stability_checks if watcher else '?'}
                for path, info in sorted(pending.items())
            ][:50],
        }


def create_from_env(service) -> Optional[WebUI]:
    """
    Create a WebUI from environment variables, or None if disabled.

    Args:
        service: The running FileWatcherService

    Returns:
        Configured WebUI, or None when WEB_UI_ENABLED is false
    """
    if not get_env_bool('WEB_UI_ENABLED', True):
        logger.info("Web UI disabled via WEB_UI_ENABLED")
        return None

    return WebUI(
        service=service,
        host=os.getenv('WEB_UI_HOST', DEFAULT_HOST),
        port=get_env_int('WEB_UI_PORT', DEFAULT_PORT),
    )


PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>inbound</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #0f1115;
    --panel: #171a21;
    --panel-2: #1d212a;
    --line: #2a2f3a;
    --text: #e6e9ef;
    --muted: #9aa3b2;
    --accent: #6ea8fe;
    --ok: #4ade80;
    --warn: #fbbf24;
    --err: #f87171;
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  header {
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
    padding: 16px 20px; border-bottom: 1px solid var(--line); background: var(--panel);
    position: sticky; top: 0; z-index: 5;
  }
  header h1 { margin: 0; font-size: 17px; letter-spacing: .3px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--ok); }
  .dot.stale { background: var(--err); }
  .spacer { flex: 1; }
  header .meta { color: var(--muted); font-size: 12px; font-family: var(--mono); }
  main { padding: 20px; max-width: 1240px; margin: 0 auto; display: grid; gap: 16px; }
  .grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
  section {
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px; overflow: hidden;
  }
  section > h2 {
    margin: 0; padding: 11px 14px; font-size: 12px; text-transform: uppercase;
    letter-spacing: .09em; color: var(--muted); border-bottom: 1px solid var(--line);
    background: var(--panel-2);
  }
  table { width: 100%; border-collapse: collapse; }
  td { padding: 7px 14px; border-bottom: 1px solid var(--line); vertical-align: top; }
  tr:last-child td { border-bottom: 0; }
  td.k { color: var(--muted); white-space: nowrap; width: 42%; }
  td.v { font-family: var(--mono); font-size: 12.5px; word-break: break-word; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 1px; background: var(--line); }
  .tile { background: var(--panel); padding: 14px; }
  .tile .n { font-size: 24px; font-family: var(--mono); }
  .tile .l { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .06em; margin-top: 3px; }
  .n.ok { color: var(--ok); } .n.err { color: var(--err); } .n.warn { color: var(--warn); }
  .controls { display: flex; gap: 8px; padding: 10px 14px; border-bottom: 1px solid var(--line); flex-wrap: wrap; align-items: center; }
  select, button {
    background: var(--panel-2); color: var(--text); border: 1px solid var(--line);
    border-radius: 6px; padding: 5px 9px; font-size: 12.5px; font-family: inherit;
  }
  button { cursor: pointer; }
  button:hover { border-color: var(--accent); }
  .log { max-height: 60vh; overflow-y: auto; }
  .ev { display: grid; grid-template-columns: 150px 78px 1fr; gap: 10px;
        padding: 7px 14px; border-bottom: 1px solid var(--line); font-size: 12.5px; }
  .ev:last-child { border-bottom: 0; }
  .ev time { color: var(--muted); font-family: var(--mono); font-size: 11.5px; }
  .ev .cat {
    font-family: var(--mono); font-size: 10.5px; text-transform: uppercase; letter-spacing: .05em;
    color: var(--muted); border: 1px solid var(--line); border-radius: 4px;
    padding: 1px 5px; height: fit-content; text-align: center;
  }
  .ev .msg { word-break: break-word; }
  .ev.success .msg { color: var(--ok); }
  .ev.warning .msg { color: var(--warn); }
  .ev.error .msg { color: var(--err); }
  .ev pre { margin: 6px 0 0; color: var(--muted); font-size: 11.5px; white-space: pre-wrap; }
  .empty { padding: 24px 14px; color: var(--muted); text-align: center; }
  @media (max-width: 620px) { .ev { grid-template-columns: 1fr; gap: 2px; } }
</style>
</head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <h1>inbound</h1>
  <span class="meta" id="uptime"></span>
  <span class="spacer"></span>
  <span class="meta" id="refreshed"></span>
</header>

<main>
  <section>
    <h2>Totals</h2>
    <div class="tiles" id="tiles"></div>
  </section>

  <div class="grid">
    <section>
      <h2>Live status</h2>
      <table id="live"></table>
    </section>
    <section>
      <h2>Settings</h2>
      <table id="settings"></table>
    </section>
  </div>

  <section id="pending-section" hidden>
    <h2>Files still growing</h2>
    <table id="pending"></table>
  </section>

  <section>
    <h2>Activity log</h2>
    <div class="controls">
      <select id="f-cat">
        <option value="">All sources</option>
        <option value="scan">Scan</option>
        <option value="discord">Discord</option>
        <option value="cn4m">cn4m</option>
        <option value="symmetry">symmetry</option>
        <option value="service">Service</option>
      </select>
      <select id="f-lvl">
        <option value="">All levels</option>
        <option value="success">Success</option>
        <option value="info">Info</option>
        <option value="warning">Warning</option>
        <option value="error">Error</option>
      </select>
      <button id="pause">Pause auto-refresh</button>
    </div>
    <div class="log" id="log"></div>
  </section>
</main>

<script>
const TILES = [
  ['scans', 'Scans', ''],
  ['files_discovered', 'Discovered', 'ok'],
  ['files_deleted', 'Removed', ''],
  ['discord_sent', 'Discord sent', 'ok'],
  ['discord_failed', 'Discord failed', 'err'],
  ['status_sent', 'cn4m sent', 'ok'],
  ['status_failed', 'cn4m failed', 'err'],
  ['rescan_sent', 'symmetry sent', 'ok'],
  ['rescan_failed', 'symmetry failed', 'err'],
  ['errors', 'Errors', 'err'],
];

let live = true;

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

function rows(el, obj) {
  el.innerHTML = Object.entries(obj)
    .map(([k, v]) => `<tr><td class="k">${esc(k)}</td><td class="v">${esc(v)}</td></tr>`)
    .join('');
}

function localTime(iso) {
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString(undefined, { hour12: false });
}

async function loadStatus() {
  const r = await fetch('api/status');
  const d = await r.json();

  document.getElementById('uptime').textContent = 'up ' + d.service.uptime;
  document.getElementById('dot').className = 'dot' + (d.service.running ? '' : ' stale');

  document.getElementById('tiles').innerHTML = TILES.map(([key, label, cls]) => {
    const n = d.counters[key] || 0;
    const tone = (n > 0 && cls) ? cls : '';
    return `<div class="tile"><div class="n ${tone}">${n}</div><div class="l">${label}</div></div>`;
  }).join('');

  const liveCopy = Object.assign({}, d.live);
  if (liveCopy['Last check'] && liveCopy['Last check'] !== 'not yet') {
    liveCopy['Last check'] = localTime(liveCopy['Last check']);
  }
  rows(document.getElementById('live'), liveCopy);
  rows(document.getElementById('settings'), d.settings);

  const sec = document.getElementById('pending-section');
  if (d.pending_files.length) {
    sec.hidden = false;
    document.getElementById('pending').innerHTML = d.pending_files.map(f =>
      `<tr><td class="v">${esc(f.path)}</td><td class="v">${esc(f.size)}</td>` +
      `<td class="k">${f.stable_checks} of ${f.required_checks} stable checks</td></tr>`
    ).join('');
  } else {
    sec.hidden = true;
  }

  document.getElementById('refreshed').textContent =
    'refreshed ' + new Date().toLocaleTimeString(undefined, { hour12: false });
}

async function loadEvents() {
  const cat = document.getElementById('f-cat').value;
  const lvl = document.getElementById('f-lvl').value;
  const qs = new URLSearchParams({ limit: '200' });
  if (cat) qs.set('category', cat);
  if (lvl) qs.set('level', lvl);

  const r = await fetch('api/events?' + qs);
  const d = await r.json();
  const log = document.getElementById('log');

  if (!d.events.length) {
    log.innerHTML = '<div class="empty">Nothing logged yet.</div>';
    return;
  }

  log.innerHTML = d.events.map(e => `
    <div class="ev ${esc(e.level)}">
      <time>${esc(localTime(e.time))}</time>
      <span class="cat">${esc(e.category)}</span>
      <div class="msg">${esc(e.message)}${e.detail ? `<pre>${esc(e.detail)}</pre>` : ''}</div>
    </div>`).join('');
}

async function refresh() {
  try {
    await Promise.all([loadStatus(), loadEvents()]);
  } catch (err) {
    document.getElementById('dot').className = 'dot stale';
  }
}

document.getElementById('f-cat').onchange = loadEvents;
document.getElementById('f-lvl').onchange = loadEvents;
document.getElementById('pause').onclick = (e) => {
  live = !live;
  e.target.textContent = live ? 'Pause auto-refresh' : 'Resume auto-refresh';
  if (live) refresh();
};

refresh();
setInterval(() => { if (live) refresh(); }, 5000);
</script>
</body>
</html>
"""
