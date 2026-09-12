"""Fire-and-forget HTTP POSTs to the rest of the cn4m suite.

The status pusher (cn4m) and the rescan trigger (symmetry) both need the same
thing: post something small to another tool, never let that tool's absence
slow the watcher down, and stop hammering an endpoint that keeps failing.
This is that shared machinery. A subclass names its target and builds its
payload; everything about sessions, backoff, logging and shutdown lives here.

Every send runs as a detached task and swallows its errors. A failing endpoint
is left alone rather than retried every scan: 60s after the first failure, then
2, 4, 8 minutes and so on up to 30, and the first success resets it. A failure
is logged once at WARNING, then at DEBUG until it recovers.
"""

import asyncio
import logging
import os
import time
import urllib.parse
from typing import Any, Dict, Optional, Set

import aiohttp

from src.activity_log import ActivityLog

# How long to leave a failing endpoint alone, doubling per consecutive failure
BACKOFF_START = 60
BACKOFF_MAX = 1800

# An unreachable tool should cost one quick attempt, not a stalled scan
REQUEST_TIMEOUT = 5

LOCAL_HOSTS = ('localhost', '127.0.0.1', '::1', '[::1]')
DOCKER_HOST = 'host.docker.internal'


def in_docker() -> bool:
    """Whether this process is running inside a container."""
    return os.path.exists('/.dockerenv')


def default_host() -> str:
    """
    The hostname that reaches a sibling tool published on the Docker host.

    Inside a container, localhost is the container itself, so a tool running
    on the host or in another container is reached through the host gateway.
    Outside Docker, localhost is right.

    Returns:
        'host.docker.internal' inside Docker, 'localhost' otherwise
    """
    return DOCKER_HOST if in_docker() else 'localhost'


def network_hint(url: str) -> str:
    """
    Explain the usual reason a URL fails, when it is a Docker networking one.

    Args:
        url: The configured URL

    Returns:
        Sentence to append to the failure log, or '' when not applicable
    """
    host = (urllib.parse.urlparse(url).hostname or '').lower()

    if in_docker() and host in LOCAL_HOSTS:
        return (
            " Note: inside a container, localhost is the container itself, not the "
            f"machine running Docker. If the tool runs on the host, use "
            f"{DOCKER_HOST} as the host; if it is another container on a shared "
            "network, use its service name."
        )

    if not in_docker() and host == DOCKER_HOST:
        return (
            f" Note: {DOCKER_HOST} is only guaranteed to resolve from inside a "
            "container. Running directly on the host, use localhost."
        )

    return ''


class Pusher:
    """Base for fire-and-forget POSTs with backoff.

    Subclasses set the class attributes below and implement ``send``, which
    should build a payload and hand it to ``_submit``.
    """

    # How the target is named in log lines, e.g. "status endpoint"
    label = 'endpoint'

    # The setting that configures it, and what it is for, for the connect log
    env_var = 'URL'
    purpose = 'updates'

    # Activity log category, counter prefix, and how a delivery is described
    category = 'service'
    counter = 'push'
    sent_verb = 'Sent'

    # Subclasses log under their own module name
    log = logging.getLogger(__name__)

    def __init__(
        self,
        url: Optional[str],
        timeout: int = REQUEST_TIMEOUT,
        activity_log: Optional[ActivityLog] = None,
    ):
        """
        Initialize the pusher.

        Args:
            url: Target URL, or empty/None to disable sends entirely
            timeout: Per-request timeout in seconds
            activity_log: Optional activity log to record attempts into
        """
        self.url = (url or '').strip()
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
        """Whether sends are configured at all."""
        return bool(self.url)

    async def connect(self) -> None:
        """Create the HTTP session."""
        if not self.enabled:
            self.log.info(f"{self.env_var} is empty, {self.purpose} are disabled")
            return

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
            self.log.info(f"{self.purpose} will go to {self.url}")

    async def drain(self, timeout: Optional[float] = None) -> None:
        """
        Give in-flight sends a moment to finish before the process ends.

        Waits a little longer than a request is allowed to take, so a send that
        was about to succeed is not cut off at the finish line. This is what
        makes the send from the last scan before a shutdown signal arrive.

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
        """Drain in-flight sends, then release the session."""
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
        self.log.debug(
            f"skipping send: {self.url} has failed "
            f"{self._failures} time(s), next try in {waiting:.0f}s"
        )
        return True

    def _ready(self) -> bool:
        """
        Whether a send should go ahead right now.

        Returns:
            True if configured and not backing off
        """
        return self.enabled and not self._in_backoff()

    def _submit(self, payload: Any, description: str, headers: Optional[Dict[str, str]] = None) -> None:
        """
        Queue one POST. Returns immediately.

        Args:
            payload: Form dict, aiohttp.FormData, or anything aiohttp accepts as data
            description: One line describing the send, for the log and status page
            headers: Optional extra request headers
        """
        task = asyncio.ensure_future(self._post(payload, description, headers))
        self._sending = {t for t in self._sending if not t.done()}
        self._sending.add(task)
        task.add_done_callback(self._sending.discard)

    async def _post(self, payload: Any, description: str, headers: Optional[Dict[str, str]]) -> None:
        """
        Send one POST, swallowing every error.

        Args:
            payload: Request body
            description: One line describing the send
            headers: Optional extra request headers
        """
        self.last_message = description

        if self._session is None or self._session.closed:
            return

        try:
            async with self._session.post(self.url, data=payload, headers=headers) as response:
                # Drain so the connection can be reused
                await response.text()

                if not 200 <= response.status < 300:
                    self._on_failure(f"responded {response.status} {response.reason}")
                    return

        except asyncio.TimeoutError:
            self._on_failure(f"timed out after {self.timeout}s")
        except aiohttp.ClientError as e:
            self._on_failure(f"is unreachable ({e})")
        except Exception as e:
            # Never let a send reach the watcher loop
            self._on_failure(f"failed: {e}")
        else:
            self._on_success(description)

    def _on_success(self, description: str) -> None:
        """
        Reset backoff and record a delivery.

        Args:
            description: What was delivered
        """
        self._failures = 0
        self._retry_at = 0.0
        self.last_result = 'ok'

        if self._failing:
            self._failing = False
            self.log.info(f"{self.label} {self.url} is reachable again")

        self.log.debug(f"{self.sent_verb.lower()}: {description}")

        if self.activity_log:
            self.activity_log.record(
                self.category, f"{self.sent_verb}: {description}", level='success'
            )
            self.activity_log.bump(f"{self.counter}_sent")

    def _on_failure(self, reason: str) -> None:
        """
        Apply backoff and log the failure once, then at DEBUG until it recovers.

        Args:
            reason: Why the send failed, phrased to follow the URL
        """
        self._failures += 1
        pause = min(BACKOFF_MAX, BACKOFF_START * (2 ** (self._failures - 1)))
        self._retry_at = time.monotonic() + pause
        self.last_result = f"{reason} (failure {self._failures})"

        if not self._failing:
            self._failing = True
            self.log.warning(
                f"{self.label} {self.url} {reason} - pausing {self.purpose} for "
                f"{pause}s, then retrying with a longer gap each time."
                f"{network_hint(self.url)}"
            )
        else:
            self.log.debug(
                f"{self.label} {self.url} {reason} "
                f"(failure {self._failures}, next try in {pause}s)"
            )

        if self.activity_log:
            self.activity_log.bump(f"{self.counter}_failed")

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
