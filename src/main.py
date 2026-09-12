"""Main entry point for the Discord File Watcher service."""

import asyncio
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

from src import rescan_trigger, status_reporter, web_ui
from src.activity_log import ActivityLog, ActivityLogHandler
from src.discord_webhook import DiscordWebhookNotifier
from src.file_watcher import FileWatcher, create_from_env
from src.state_manager import StateManager
from src.utils import get_env_int, get_env_var, parse_interval

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.getenv('LOG_FILE', '/watcher.log'), encoding='utf-8')
    ]
)

logger = logging.getLogger(__name__)


class FileWatcherService:
    """Main service orchestrator."""

    def __init__(self):
        """Initialize the service."""
        self.running = True
        self.notifier = None
        self.state_manager = None
        self.file_watcher = None
        self.check_interval = None

        # Shared by the watcher loop and the web UI
        self.activity_log = ActivityLog()
        self.status_pusher = None
        self.rescan_trigger = None
        self.web_ui = None

        # Wall clock of the last completed cycle, used for the "next check" countdown
        self.last_check_wallclock = None

        # How a manual scan differs from a scheduled one: fewer stable checks,
        # taken a few seconds apart instead of a full interval apart
        self.manual_stability_checks = 1
        self.manual_settle_seconds = 5

        # What the loop is doing right now, for the status page
        self.scan_state = 'idle'

        self._loop = None
        self._wake = None            # set to interrupt the sleep
        self._wake_reason = None     # 'manual' when the status page asked for a scan

    def setup(self):
        """Setup the service with configuration from environment."""
        try:
            # Mirror warnings and errors from anywhere in the service into the web UI
            logging.getLogger().addHandler(ActivityLogHandler(self.activity_log))

            # Load required environment variables
            watch_folder = get_env_var('WATCH_FOLDER', required=True)
            state_file = get_env_var('STATE_FILE', required=True)
            webhook_url = get_env_var('DISCORD_WEBHOOK_URL', required=True)
            check_interval_str = get_env_var('CHECK_INTERVAL', required=True)

            # Parse check interval
            self.check_interval = parse_interval(check_interval_str)
            logger.info(f"Check interval: {self.check_interval} seconds")

            # A manual scan needs at least one steady check: a file never seen
            # before has nothing to be compared against
            self.manual_stability_checks = max(1, get_env_int('MANUAL_STABILITY_CHECKS', 1))
            self.manual_settle_seconds = max(1, get_env_int('MANUAL_SETTLE_SECONDS', 5))

            # Initialize components
            self.state_manager = StateManager(state_file)
            self.file_watcher = create_from_env()

            # Create Discord webhook notifier
            self.notifier = DiscordWebhookNotifier(webhook_url)

            # Status updates to cn4m and the local status page
            self.status_pusher = status_reporter.create_from_env(self.activity_log)
            self.rescan_trigger = rescan_trigger.create_from_env(self.activity_log)
            self.web_ui = web_ui.create_from_env(self)

            logger.info("Service initialized successfully")
            logger.info(f"Watching folder: {watch_folder}")
            logger.info(f"State file: {state_file}")

            self.activity_log.record(
                'service',
                f"Service started, watching {watch_folder} every {self.check_interval}s",
                level='success',
            )

        except Exception as e:
            logger.error(f"Failed to setup service: {e}")
            raise

    async def run(self):
        """Run the main service loop."""
        self._loop = asyncio.get_event_loop()
        self._wake = asyncio.Event()

        # Open the webhook session
        logger.info("Opening Discord webhook session...")
        await self.notifier.connect()
        await self.status_pusher.connect()
        await self.rescan_trigger.connect()
        await self.start_web_ui()

        logger.info("Webhook ready, starting file watcher loop...")

        try:
            # Main loop. Everything that touches state runs here, in order:
            # the status page only ever asks for a scan, never runs one.
            while self.running:
                reason = self._take_wake_reason()
                try:
                    if reason == 'manual':
                        await self.manual_scan()
                    else:
                        self.scan_state = 'scheduled scan'
                        await self.check_cycle()

                except Exception as e:
                    logger.error(f"Error in check cycle: {e}", exc_info=True)
                finally:
                    self.scan_state = 'idle'

                # Sleep until next check, waking early for shutdown or a manual scan
                logger.debug(f"Sleeping for {self.check_interval} seconds...")
                await self._sleep_until_wake(self.check_interval)

        finally:
            await self.notifier.close()
            await self.status_pusher.close()
            await self.rescan_trigger.close()
            if self.web_ui:
                await self.web_ui.stop()

    async def _sleep_until_wake(self, seconds: float):
        """
        Sleep, but return early if something sets the wake event.

        Args:
            seconds: How long to sleep if nothing wakes the loop
        """
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    def _take_wake_reason(self):
        """
        Read and clear why the loop was woken.

        Returns:
            'manual' if a scan was requested, else None for a scheduled pass
        """
        reason, self._wake_reason = self._wake_reason, None
        return reason

    def request_scan(self) -> bool:
        """
        Ask the loop to run a manual scan as soon as it can. Safe from any task.

        A request that arrives during a scan is picked up right after it, so
        clicking while a scheduled scan is mid-way loses nothing.

        Returns:
            True if queued, False if a manual scan is already pending or running
        """
        if self._wake_reason == 'manual' or self.scan_state.startswith('manual'):
            return False

        self._wake_reason = 'manual'
        if self._loop and self._wake:
            self._loop.call_soon_threadsafe(self._wake.set)

        self.activity_log.record('scan', 'Manual scan requested from the status page', level='info')
        return True

    async def manual_scan(self):
        """
        Scan now, with the settle period compressed.

        The stability logic is unchanged - a file still has to hold its size
        across consecutive checks - but the checks come seconds apart rather
        than a full interval apart, and fewer are needed. Runs one pass to
        discover, then one per required check, stopping as soon as nothing is
        left pending. Quiet hours do not apply: a click is intent.
        """
        checks = self.manual_stability_checks
        settle = self.manual_settle_seconds
        passes = checks + 1

        logger.info(
            f"Manual scan: up to {passes} passes {settle}s apart, "
            f"{checks} stable check(s) required"
        )
        self.activity_log.bump('manual_scans')

        found = 0
        for pass_no in range(1, passes + 1):
            self.scan_state = f"manual scan (pass {pass_no} of {passes})"
            result = await self.check_cycle(required_checks=checks, force=True)
            found += result['new'] + result['refreshed']

            if not result['pending'] or pass_no == passes or not self.running:
                break

            self.scan_state = f"manual scan (settling {settle}s before pass {pass_no + 1})"
            await asyncio.sleep(settle)

        still_pending = len(self.state_manager.get_pending_files())
        if found:
            summary = f"Manual scan announced {found} file(s)"
            level = 'success'
        else:
            summary = "Manual scan found nothing new"
            level = 'info'
        if still_pending:
            summary += f", {still_pending} still growing"

        logger.info(summary)
        self.activity_log.record('scan', summary, level=level)

    async def start_web_ui(self):
        """Start the status page, leaving the watcher running if the port is taken."""
        if not self.web_ui:
            return

        try:
            await self.web_ui.start()
            self.activity_log.record(
                'service',
                f"Web UI listening on port {self.web_ui.port}",
                level='success',
            )
        except Exception as e:
            logger.error(f"Failed to start web UI on port {self.web_ui.port}: {e}")
            self.web_ui = None

    async def check_cycle(self, required_checks: int = None, force: bool = False) -> dict:
        """
        Execute one check cycle.

        Args:
            required_checks: Stable checks a growing file needs before it is
                announced, defaulting to STABILITY_CHECKS
            force: Scan even during quiet hours

        Returns:
            Counts of what happened: new, refreshed, removed, pending, skipped
        """
        now = datetime.now(timezone.utc)
        logger.info("Starting check cycle...")
        empty = {'new': 0, 'refreshed': 0, 'removed': 0, 'pending': 0, 'skipped': True}

        # Check if in quiet hours
        in_quiet_hours = self.file_watcher.is_quiet_hours()
        was_in_quiet_hours = self.state_manager.was_in_quiet_hours()

        # Update quiet hours status in state
        self.state_manager.set_quiet_hours_status(in_quiet_hours)

        if in_quiet_hours and not force:
            logger.info("Currently in quiet hours, skipping check")
            return empty

        self.activity_log.bump('scans')

        # Scan folder for files
        current_files = self.file_watcher.scan_folder()
        tracked_files = self.state_manager.get_all_files()

        self.reconcile_pending(current_files, tracked_files)

        # Detect changes
        new_files, modified_files, deleted_files = self.file_watcher.detect_changes(
            current_files,
            tracked_files,
        )

        # Process deleted files
        if deleted_files:
            logger.info(f"Processing {len(deleted_files)} deleted files")
            sent = await self.notifier.send_deleted_files_notification(deleted_files)
            self.record_discord_result(sent, f"{len(deleted_files)} deleted file(s)")
            self.activity_log.bump('files_deleted', len(deleted_files))

            # Remove deleted files from state
            for file_data in deleted_files:
                self.state_manager.remove_file_state(file_data['path'])
                self.state_manager.remove_pending_file(file_data['path'])

        # Process new and modified files
        stable_new, stable_modified = await self.process_files(
            new_files, modified_files, required_checks=required_checks
        )

        # Tell cn4m, but only when the scan actually had news
        message = status_reporter.discovery_message(
            new_count=len(stable_new),
            refreshed_count=len(stable_modified),
            removed_count=len(deleted_files),
        )
        if message:
            self.status_pusher.send(message)

        # Send summary if coming out of quiet hours and found files
        if was_in_quiet_hours and not in_quiet_hours:
            logger.info("Exiting quiet hours, checking for summary notification...")
            # Note: Since we skip checks during quiet hours, any files that were
            # added during quiet hours would be discovered now and handled
            # through the normal new/modified file flow above.

        # Update last check timestamp
        self.state_manager.update_last_check(now)
        self.state_manager.save_state()
        self.last_check_wallclock = time.time()

        logger.info("Check cycle completed")

        return {
            'new': len(stable_new),
            'refreshed': len(stable_modified),
            'removed': len(deleted_files),
            'pending': len(self.state_manager.get_pending_files()),
            'skipped': False,
        }

    async def process_files(self, new_files: list, modified_files: list, required_checks: int = None):
        """
        Process new and modified files for stability and send notifications.

        Args:
            new_files: List of new file metadata
            modified_files: List of modified file metadata
            required_checks: Stable checks needed, defaulting to STABILITY_CHECKS

        Returns:
            Tuple of (stable new files, stable modified files)
        """
        if required_checks is None:
            required_checks = self.file_watcher.stability_checks

        all_files = new_files + modified_files
        modified_paths = {f['relative_path'] for f in modified_files}
        stable_files = []
        pending_count = 0

        for file_data in all_files:
            path = file_data['relative_path']
            size = file_data['size']
            mime_type = file_data['mime_type']
            modified_time = datetime.fromisoformat(file_data['modified_time'])

            # Check if we should track this file for growth
            if not self.file_watcher.should_check_for_growth(size):
                # Small file, mark as stable immediately
                logger.debug(f"File {path} is below growth threshold, marking as stable")
                self.state_manager.update_file_state(
                    path,
                    size,
                    mime_type,
                    modified_time,
                    status='stable',
                )
                stable_files.append(file_data)
                continue

            # Check file stability
            pending_file = self.state_manager.get_pending_files().get(path)
            is_stable = False

            if pending_file:
                # File is already being tracked for growth. The comparison is
                # against the size seen on the previous check, and passing the
                # current size back keeps it that way for the next one.
                previous_size = pending_file['size']
                unchanged = self.file_watcher.check_file_stability(path, size, previous_size)

                settled = self.state_manager.update_pending_file_stability(
                    path,
                    stable=unchanged,
                    size=size,
                    required_checks=required_checks,
                )

                if settled:
                    # Size held steady for the full run of checks
                    is_stable = True
                    self.state_manager.remove_pending_file(path)
                    logger.info(f"File {path} is now stable after growth")
                else:
                    # Either still growing, or not yet held steady long enough
                    is_stable = False
                    pending_count += 1

            else:
                # New file or not in pending, check against tracked state
                tracked = self.state_manager.get_file_state(path)

                if tracked:
                    # Modified file, check if size changed
                    is_stable = self.file_watcher.check_file_stability(
                        path,
                        size,
                        tracked['size'],
                    )
                else:
                    # Completely new file, assume unstable initially
                    is_stable = False

                if not is_stable:
                    # Add to pending
                    self.state_manager.add_pending_file(path, size, datetime.now(timezone.utc))
                    pending_count += 1
                    logger.debug(f"Added file {path} to pending (still growing)")

            if is_stable:
                # Mark as stable in state
                self.state_manager.update_file_state(
                    path,
                    size,
                    mime_type,
                    modified_time,
                    status='stable',
                )
                stable_files.append(file_data)

        # Send notifications for stable files
        if stable_files:
            # symmetry first: it is non-blocking, and the point is that it
            # links these before its own scan would have got round to them
            self.rescan_trigger.send(f['relative_path'] for f in stable_files)

            logger.info(f"Sending notifications for {len(stable_files)} stable file(s)")
            sent = await self.notifier.send_new_files_notification(stable_files)
            self.record_discord_result(
                sent,
                f"{len(stable_files)} new file(s)",
                detail='\n'.join(f['relative_path'] for f in sorted(
                    stable_files, key=lambda f: f['relative_path']
                )[:25]),
            )
            self.activity_log.bump('files_discovered', len(stable_files))

        if pending_count > 0:
            logger.info(f"Tracking {pending_count} file(s) still growing")

        stable_new = [f for f in stable_files if f['relative_path'] not in modified_paths]
        stable_modified = [f for f in stable_files if f['relative_path'] in modified_paths]

        return stable_new, stable_modified

    def reconcile_pending(self, current_files: dict, tracked_files: dict):
        """
        Clear pending entries that can never settle.

        Without this, two kinds of entry accumulate forever and make the growing
        file count meaningless: files already promoted to stable, and files
        renamed or removed before they settled (delete detection never sees
        those, because it only looks at files that reached the tracked list).

        Args:
            current_files: Files present in the scan just completed
            tracked_files: Files already tracked as stable
        """
        if not current_files and tracked_files:
            # An empty scan against a non-empty state means the watch folder is
            # unreadable, not that everything vanished. Leave the state alone.
            logger.warning(
                "Scan returned no files while tracking "
                f"{len(tracked_files)}, skipping pending cleanup"
            )
            return

        already_stable, vanished = self.state_manager.reconcile_pending(set(current_files))

        if already_stable:
            self.activity_log.record(
                'scan',
                f"Cleared {len(already_stable)} stale pending entr"
                f"{'y' if len(already_stable) == 1 else 'ies'} for files already stable",
                level='info',
                detail='\n'.join(sorted(already_stable)[:25]),
            )

        if vanished:
            self.activity_log.record(
                'scan',
                f"Cleared {len(vanished)} pending entr"
                f"{'y' if len(vanished) == 1 else 'ies'} for files that disappeared "
                "before settling",
                level='info',
                detail='\n'.join(sorted(vanished)[:25]),
            )

    def record_discord_result(self, sent: bool, what: str, detail: str = None):
        """
        Record the outcome of a Discord submission in the activity log.

        Args:
            sent: Whether the webhook accepted the message
            what: Short description of what was submitted
            detail: Optional longer text shown under the entry
        """
        if sent:
            self.activity_log.record(
                'discord', f"Submitted {what} to Discord", level='success', detail=detail
            )
            self.activity_log.bump('discord_sent')
        else:
            # The reason was already logged by the notifier and mirrored as an error
            self.activity_log.record(
                'discord', f"Failed to submit {what} to Discord", level='error', detail=detail
            )
            self.activity_log.bump('discord_failed')

    def stop(self):
        """Stop the service gracefully."""
        logger.info("Stopping service...")
        self.running = False

        # Wake the loop out of its sleep so shutdown is not delayed a whole interval
        if self._loop and self._wake:
            self._loop.call_soon_threadsafe(self._wake.set)


def main():
    """Main entry point."""
    logger.info("Starting Discord File Watcher Service...")

    try:
        service = FileWatcherService()
        service.setup()

        def signal_handler(signum, frame):
            """Handle shutdown signals."""
            logger.info(f"Received signal {signum}, shutting down...")
            service.stop()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # Run the service
        asyncio.run(service.run())

    except Exception as e:
        logger.error(f"Service failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
