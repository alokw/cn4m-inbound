"""Push short status updates to cn4m, the parent system.

Equivalent to:

    curl -X POST http://<cn4m-host>:2640/suite/status \
         -d app=inbound -d message="Discovered 5 new assets" -d level=ok

One line per scan that actually had news. Everything here is best effort: a
status update is never worth delaying or interrupting a scan for, so a cn4m
that is slow, down, or missing entirely changes nothing about watching. The
mechanics of that - detached sends, backoff, draining on shutdown - are shared
with the symmetry rescan trigger and live in ``src.pusher``.

``level`` is one of ``idle``, ``ok``, ``working``, ``warning``, ``blocked`` or
``error``. cn4m owns that list and what each one means - see ``LEVELS`` in
``cn4m/app/suite_status.py`` and the Suite status feed section of cn4m's README.
Nothing here validates it: an unrecognised level is not an error, it is
lowercased and falls back to ``idle``, so a typo costs a colour rather than a
message and nothing is reported back.
"""

import logging
import os
from typing import Optional

from src.activity_log import ActivityLog
from src.pusher import REQUEST_TIMEOUT, Pusher, default_host

logger = logging.getLogger(__name__)

# Name this service reports itself as
DEFAULT_APP_NAME = 'inbound'


def default_status_url() -> str:
    """
    Where cn4m listens, from wherever this service happens to be running.

    Returns:
        URL using host.docker.internal inside Docker, localhost otherwise
    """
    return f"http://{default_host()}:2640/suite/status"


class StatusPusher(Pusher):
    """Fire-and-forget POSTs to cn4m's /suite/status."""

    label = 'status endpoint'
    env_var = 'STATUS_URL'
    purpose = 'cn4m status updates'
    category = 'cn4m'
    counter = 'status'
    sent_verb = 'Status sent'
    log = logger

    def __init__(
        self,
        url: Optional[str],
        app: str = DEFAULT_APP_NAME,
        level: str = 'ok',
        timeout: int = REQUEST_TIMEOUT,
        activity_log: Optional[ActivityLog] = None,
    ):
        """
        Initialize the pusher.

        Args:
            url: cn4m status endpoint, or empty/None to disable updates
            app: Name this service reports itself as
            level: Default cn4m level tag. "ok" rather than "working" because
                every update this service sends describes a scan that has
                already finished - there is no in-progress state to report
            timeout: Per-request timeout in seconds
            activity_log: Optional activity log to record attempts into
        """
        super().__init__(url, timeout=timeout, activity_log=activity_log)
        self.app = app
        self.level = level

    def send(self, message: str, level: Optional[str] = None) -> None:
        """
        Queue a status update. Returns immediately.

        Args:
            message: Short one-line summary (e.g. 'Discovered 5 new assets')
            level: cn4m level tag, defaulting to the configured one
        """
        if not message or not self._ready():
            return

        payload = {'app': self.app, 'message': message, 'level': level or self.level}
        self._submit(payload, message)


def discovery_message(
    new_count: int,
    refreshed_count: int = 0,
    removed_count: int = 0,
) -> Optional[str]:
    """
    The short line describing what a scan just found, or None if it found nothing.

    A quiet scan stays quiet, so cn4m sees traffic when there is news rather
    than once per interval forever.

    Args:
        new_count: Number of newly discovered stable files
        refreshed_count: Number of already known files that changed
        removed_count: Number of files that disappeared

    Returns:
        Message string, or None if nothing is worth reporting
    """
    parts = []

    if new_count:
        parts.append(
            "Discovered %d new asset%s" % (new_count, "" if new_count == 1 else "s")
        )

    if refreshed_count:
        parts.append("refreshed %d" % refreshed_count)

    if removed_count:
        text = "Removed %d file%s" % (removed_count, "" if removed_count == 1 else "s")
        parts.append(text if not parts else text.lower())

    if not parts:
        return None

    return ', '.join(parts)


def create_from_env(activity_log: Optional[ActivityLog] = None) -> StatusPusher:
    """
    Create a StatusPusher from environment variables.

    Args:
        activity_log: Optional activity log to record attempts into

    Returns:
        Configured StatusPusher
    """
    return StatusPusher(
        url=os.getenv('STATUS_URL', default_status_url()),
        app=os.getenv('STATUS_APP_NAME', DEFAULT_APP_NAME),
        activity_log=activity_log,
    )
