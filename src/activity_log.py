"""In-memory activity log shared between the watcher and the web UI."""

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# How many events to keep in memory before the oldest are dropped
DEFAULT_MAX_EVENTS = 500


class ActivityLog:
    """A bounded, thread-safe ring buffer of recent events plus running totals.

    The watcher records events as they happen and the web UI reads them back.
    Nothing here touches disk — the log file remains the durable record.
    """

    def __init__(self, max_events: int = DEFAULT_MAX_EVENTS):
        """
        Initialize the activity log.

        Args:
            max_events: Maximum number of events kept in memory
        """
        self._events: deque = deque(maxlen=max_events)
        self._lock = threading.Lock()
        self._counters: Dict[str, int] = {
            'scans': 0,
            'files_discovered': 0,
            'files_deleted': 0,
            'discord_sent': 0,
            'discord_failed': 0,
            'status_sent': 0,
            'status_failed': 0,
            'rescan_sent': 0,
            'rescan_failed': 0,
            'errors': 0,
        }
        self._next_id = 1
        self.started_at = datetime.now(timezone.utc)

    def record(
        self,
        category: str,
        message: str,
        level: str = 'info',
        detail: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Record a single event.

        Args:
            category: Coarse grouping ('scan', 'discord', 'cn4m', 'service')
            message: Human readable one-liner
            level: 'success', 'info', 'warning', or 'error'
            detail: Optional longer text (file list, error body)

        Returns:
            The stored event dictionary
        """
        event = {
            'id': 0,
            'time': datetime.now(timezone.utc).isoformat(),
            'category': category,
            'level': level,
            'message': message,
            'detail': detail,
        }

        with self._lock:
            event['id'] = self._next_id
            self._next_id += 1
            self._events.append(event)

        return event

    def bump(self, counter: str, amount: int = 1) -> None:
        """
        Increment a running total.

        Args:
            counter: Counter name (unknown names are created on first use)
            amount: How much to add
        """
        with self._lock:
            self._counters[counter] = self._counters.get(counter, 0) + amount

    def events(
        self,
        limit: Optional[int] = None,
        category: Optional[str] = None,
        level: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get recent events, newest first.

        Args:
            limit: Maximum number of events to return
            category: Only return events in this category
            level: Only return events at this level

        Returns:
            List of event dictionaries, newest first
        """
        with self._lock:
            items = list(self._events)

        items.reverse()

        if category:
            items = [e for e in items if e['category'] == category]
        if level:
            items = [e for e in items if e['level'] == level]
        if limit is not None:
            items = items[:limit]

        return items

    def counters(self) -> Dict[str, int]:
        """
        Get a copy of the running totals.

        Returns:
            Dictionary of counter name to value
        """
        with self._lock:
            return dict(self._counters)

    def uptime_seconds(self) -> float:
        """
        Get how long this log (and so the service) has been running.

        Returns:
            Seconds since the log was created
        """
        return (datetime.now(timezone.utc) - self.started_at).total_seconds()


class ActivityLogHandler(logging.Handler):
    """Logging handler that mirrors warnings and errors into an ActivityLog.

    This catches failures raised anywhere in the service without needing an
    explicit record() call at every site. Successes are recorded explicitly by
    the code that knows what succeeded.
    """

    def __init__(self, activity_log: ActivityLog, level: int = logging.WARNING):
        """
        Initialize the handler.

        Args:
            activity_log: Log to mirror records into
            level: Minimum level to mirror (default WARNING)
        """
        super().__init__(level=level)
        self.activity_log = activity_log

    def emit(self, record: logging.LogRecord) -> None:
        """
        Mirror one log record into the activity log.

        Args:
            record: The record being logged
        """
        try:
            level = 'error' if record.levelno >= logging.ERROR else 'warning'
            detail = None
            if record.exc_info:
                detail = self.format(record)

            self.activity_log.record(
                category=_category_for(record.name),
                message=record.getMessage(),
                level=level,
                detail=detail,
            )

            if level == 'error':
                self.activity_log.bump('errors')

        except Exception:
            # A broken activity log must never break logging itself
            self.handleError(record)


def _category_for(logger_name: str) -> str:
    """
    Map a logger name onto an activity category.

    Args:
        logger_name: Dotted logger name (e.g. 'src.discord_webhook')

    Returns:
        Category string used by the web UI
    """
    if 'discord' in logger_name:
        return 'discord'
    if 'status' in logger_name:
        return 'cn4m'
    if 'rescan' in logger_name:
        return 'symmetry'
    if 'watcher' in logger_name or 'state' in logger_name:
        return 'scan'
    return 'service'
