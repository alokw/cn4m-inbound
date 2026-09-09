"""Push short status updates to cn4m, the parent system.

Equivalent to:

    curl -X POST http://<cn4m-host>:2640/suite/status \
         -d app=inbound -d message="Discovered 5 new assets" -d level=ok

One line per scan that actually had news. Everything here is best effort: a
status update is never worth delaying or interrupting a scan for, so a cn4m
that is slow, down, or missing entirely changes nothing about watching.

``level`` is one of ``idle``, ``ok``, ``working``, ``warning``, ``blocked`` or
``error``. cn4m owns that list and what each one means - see ``LEVELS`` in
``cn4m/app/suite_status.py`` and the Suite status feed section of cn4m's README.
Nothing here validates it: an unrecognised level is not an error, it is
lowercased and falls back to ``idle``, so a typo costs a colour rather than a
message and nothing is reported back.
"""

import asyncio
import logging
import os
import time
import urllib.parse
from typing import Optional, Set

import aiohttp

from src.activity_log import ActivityLog

logger = logging.getLogger(__name__)

# Where cn4m listens when it shares a machine with this service
DEFAULT_STATUS_URL = 'http://localhost:2640/suite/status'

# Name this service reports itself as
DEFAULT_APP_NAME = 'inbound'

# How long to leave a failing endpoint alone, doubling per consecutive failure
BACKOFF_START = 60
BACKOFF_MAX = 1800

# An unreachable cn4m should cost one quick attempt, not a stalled scan
REQUEST_TIMEOUT = 5

LOCAL_HOSTS = ('localhost', '127.0.0.1', '::1', '[::1]')


def container_hint(url: str) -> str:
    """
    Explain the usual reason a localhost status URL fails inside Docker.

    Args:
        url: The configured status URL

    Returns:
        Sentence to append to the failure log, or '' when not applicable
    """
    if not os.path.exists('/.dockerenv'):
        return ''

    host = urllib.parse.urlparse(url).hostname or ''
    if host.lower() not in LOCAL_HOSTS:
        return ''

    return (
        " Note: inside a container, localhost is the container itself, not the "
        "machine running Docker. If cn4m runs on the host, use "
        "http://host.docker.internal:2640/suite/status; if it is another "
        "container, use its service name."
    )


class StatusPusher:
    """Fire-and-forget POSTs to cn4m's /suite/status.

    Every send runs as a detached task and swallows its errors, so nothing here
    can delay or interrupt the watcher loop. A failing endpoint is left alone
    rather than retried every scan: 60s after the first failure, then 2, 4, 8
    minutes and so on up to 30, and the first success resets it.
    """

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
        self.url = (url or '').strip()
        self.app = app
        self.level = level
        self.timeout = timeout
        self.activity_log = activity_log

        self._session: Optional[aiohttp.ClientSession] = None
        self._sending: Set[asyncio.Task] = set()

        self._failing = False
        self._failures = 0
        self._retry_at = 0.0

        # Surfaced on the status page
        self.last_message: Optional[str] = None
        self.last_result: Optional[str] = None

    @property
    def enabled(self) -> bool:
        """Whether updates are configured at all."""
        return bool(self.url)

    async def connect(self) -> None:
        """Create the HTTP session used for status updates."""
        if not self.enabled:
            logger.info("STATUS_URL is empty, cn4m status updates are disabled")
            return

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
            logger.info(f"cn4m status updates will go to {self.url}")

    async def drain(self, timeout: Optional[float] = None) -> None:
        """
        Give in-flight updates a moment to finish before the process ends.

        Waits a little longer than a request is allowed to take, so a send that
        was about to succeed is not cut off at the finish line. This is what
        makes the update from the last scan before a shutdown signal arrive.

        Args:
            timeout: How long to wait, defaulting to just over one request
        """
        outstanding = {t for t in self._sending if not t.done()}
        if outstanding:
            await asyncio.wait(
                outstanding,
                timeout=self.timeout + 1 if timeout is None else timeout,
            )

    async def close(self) -> None:
        """Drain in-flight updates, then release the session."""
        await self.drain()

        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _in_backoff(self) -> bool:
        """
        Whether a failing endpoint is currently being left alone.

        Returns:
            True while inside the backoff window
        """
        if not self._failures or time.monotonic() >= self._retry_at:
            return False

        waiting = self._retry_at - time.monotonic()
        logger.debug(
            f"skipping status update: {self.url} has failed "
            f"{self._failures} time(s), next try in {waiting:.0f}s"
        )
        return True

    def send(self, message: str, level: Optional[str] = None) -> None:
        """
        Queue a status update. Returns immediately.

        Args:
            message: Short one-line summary (e.g. 'Discovered 5 new assets')
            level: cn4m level tag, defaulting to the configured one
        """
        if not self.enabled or not message or self._in_backoff():
            return

        task = asyncio.ensure_future(self._post(message, level or self.level))
        self._sending = {t for t in self._sending if not t.done()}
        self._sending.add(task)
        task.add_done_callback(self._sending.discard)

    async def _post(self, message: str, level: str) -> None:
        """
        Send one update, swallowing every error.

        Args:
            message: Short one-line summary
            level: cn4m level tag
        """
        self.last_message = message

        if self._session is None or self._session.closed:
            return

        payload = {'app': self.app, 'message': message, 'level': level}

        try:
            async with self._session.post(self.url, data=payload) as response:
                # Drain so the connection can be reused
                body = (await response.text())[:2048]

                if not 200 <= response.status < 300:
                    self._on_failure(f"responded {response.status} {response.reason}")
                    return

        except asyncio.TimeoutError:
            self._on_failure(f"timed out after {self.timeout}s")
        except aiohttp.ClientError as e:
            self._on_failure(f"is unreachable ({e})")
        except Exception as e:
            # Never let a status update reach the watcher loop
            self._on_failure(f"failed: {e}")
        else:
            self._on_success(message)

    def _on_success(self, message: str) -> None:
        """
        Reset backoff and record a delivered update.

        Args:
            message: The message that was delivered
        """
        self._failures = 0
        self._retry_at = 0.0
        self.last_result = 'ok'

        if self._failing:
            self._failing = False
            logger.info(f"status endpoint {self.url} is reachable again")

        logger.debug(f"pushed status: {message}")

        if self.activity_log:
            self.activity_log.record('cn4m', f"Status sent: {message}", level='success')
            self.activity_log.bump('status_sent')

    def _on_failure(self, reason: str) -> None:
        """
        Apply backoff and log the failure once, then at DEBUG until it recovers.

        Args:
            reason: Why the update failed, phrased to follow the URL
        """
        self._failures += 1
        pause = min(BACKOFF_MAX, BACKOFF_START * (2 ** (self._failures - 1)))
        self._retry_at = time.monotonic() + pause
        self.last_result = f"{reason} (failure {self._failures})"

        if not self._failing:
            self._failing = True
            logger.warning(
                f"status endpoint {self.url} {reason} - pausing updates for "
                f"{pause}s, then retrying with a longer gap each time."
                f"{container_hint(self.url)}"
            )
        else:
            logger.debug(
                f"status endpoint {self.url} {reason} "
                f"(failure {self._failures}, next try in {pause}s)"
            )

        if self.activity_log:
            self.activity_log.bump('status_failed')

    def describe_backoff(self) -> Optional[str]:
        """
        Describe the current backoff state for the status page.

        Returns:
            Human readable description, or None when not backing off
        """
        if not self._failures:
            return None

        remaining = self._retry_at - time.monotonic()
        if remaining <= 0:
            return f"{self._failures} failure(s), retrying on next scan"

        return f"{self._failures} failure(s), next try in {int(remaining)}s"


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
        url=os.getenv('STATUS_URL', DEFAULT_STATUS_URL),
        app=os.getenv('STATUS_APP_NAME', DEFAULT_APP_NAME),
        activity_log=activity_log,
    )
