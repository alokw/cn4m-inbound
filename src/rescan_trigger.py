"""Tell symmetry a file is complete, so it links it now rather than next scan.

Equivalent to:

    curl -X POST http://<symmetry-host>:2647/api/rescan \
         -d path=1100/new_asset.mov -d path=1100/other.mov

symmetry mirrors the same folder this service watches, creating links for the
repository. On its own it waits for a file to settle before linking it; this
service already knows when a file has settled, so it vouches for the paths and
symmetry links them on sight instead of waiting out its own settle period.

One request per scan carries every file that went stable in it. The paths are
relative to the watch folder with forward slashes, which is what symmetry keys
its files on (relative to SOURCE_DIR, as POSIX) - the two tools must be rooted
at the same folder for that to line up.

Best effort, like the status pusher: symmetry may not be running, and a missing
symmetry costs one attempt and a log line, never a delayed scan. The shared
mechanics live in ``src.pusher``.
"""

import logging
import os
from typing import Iterable, Optional

import aiohttp

from src.activity_log import ActivityLog
from src.pusher import REQUEST_TIMEOUT, Pusher, default_host

logger = logging.getLogger(__name__)

# symmetry's webhook can be locked with WEBHOOK_TOKEN on its side; when it is,
# the same value has to travel in this header
TOKEN_HEADER = 'X-Webhook-Token'


def default_rescan_url() -> str:
    """
    Where symmetry listens, from wherever this service happens to be running.

    Returns:
        URL using host.docker.internal inside Docker, localhost otherwise
    """
    return f"http://{default_host()}:2647/api/rescan"


def normalize_path(relative_path: str) -> str:
    """
    Put a watch-folder-relative path in the form symmetry keys on.

    Args:
        relative_path: Path as this service tracks it, possibly with backslashes

    Returns:
        Forward-slash path with no leading or trailing slash
    """
    return relative_path.replace('\\', '/').strip('/')


class RescanTrigger(Pusher):
    """Fire-and-forget POSTs to symmetry's /api/rescan."""

    label = 'rescan endpoint'
    env_var = 'SYMMETRY_RESCAN_URL'
    purpose = 'symmetry rescan triggers'
    category = 'symmetry'
    counter = 'rescan'
    sent_verb = 'Rescan requested'
    log = logger

    def __init__(
        self,
        url: Optional[str],
        token: Optional[str] = None,
        timeout: int = REQUEST_TIMEOUT,
        activity_log: Optional[ActivityLog] = None,
    ):
        """
        Initialize the trigger.

        Args:
            url: symmetry rescan endpoint, or empty/None to disable triggers
            token: symmetry's WEBHOOK_TOKEN, if it has one configured
            timeout: Per-request timeout in seconds
            activity_log: Optional activity log to record attempts into
        """
        super().__init__(url, timeout=timeout, activity_log=activity_log)
        self.token = (token or '').strip()

    def send(self, paths: Iterable[str]) -> None:
        """
        Ask symmetry to link these paths now. Returns immediately.

        Args:
            paths: Watch-folder-relative paths of files that have gone stable
        """
        paths = sorted({normalize_path(p) for p in paths if p and p.strip()})
        if not paths or not self._ready():
            return

        # One request, one `path` field per file - symmetry reads them as a list
        form = aiohttp.FormData()
        for path in paths:
            form.add_field('path', path)

        headers = {TOKEN_HEADER: self.token} if self.token else None

        if len(paths) == 1:
            description = paths[0]
        else:
            description = f"{len(paths)} paths ({paths[0]}, ...)"

        self._submit(form, description, headers)


def create_from_env(activity_log: Optional[ActivityLog] = None) -> RescanTrigger:
    """
    Create a RescanTrigger from environment variables.

    Args:
        activity_log: Optional activity log to record attempts into

    Returns:
        Configured RescanTrigger
    """
    return RescanTrigger(
        url=os.getenv('SYMMETRY_RESCAN_URL', default_rescan_url()),
        token=os.getenv('SYMMETRY_RESCAN_TOKEN'),
        activity_log=activity_log,
    )
