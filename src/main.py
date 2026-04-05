"""Main entry point for the Discord File Watcher service."""

import asyncio
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

from src.discord_bot import DiscordFileWatcherBot
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
        self.bot = None
        self.state_manager = None
        self.file_watcher = None
        self.check_interval = None

    def setup(self):
        """Setup the service with configuration from environment."""
        try:
            # Load required environment variables
            watch_folder = get_env_var('WATCH_FOLDER', required=True)
            state_file = get_env_var('STATE_FILE', required=True)
            discord_token = get_env_var('DISCORD_BOT_TOKEN', required=True)
            channel_id = get_env_var('DISCORD_CHANNEL_ID', required=True)
            check_interval_str = get_env_var('CHECK_INTERVAL', required=True)

            # Parse check interval
            self.check_interval = parse_interval(check_interval_str)
            logger.info(f"Check interval: {self.check_interval} seconds")

            # Initialize components
            self.state_manager = StateManager(state_file)
            self.file_watcher = create_from_env()

            # Create Discord bot
            self.bot = DiscordFileWatcherBot(int(channel_id))

            logger.info("Service initialized successfully")
            logger.info(f"Watching folder: {watch_folder}")
            logger.info(f"State file: {state_file}")

            return discord_token

        except Exception as e:
            logger.error(f"Failed to setup service: {e}")
            raise

    async def run(self, discord_token: str):
        """
        Run the main service loop.

        Args:
            discord_token: Discord bot token
        """
        # Start Discord bot
        logger.info("Starting Discord bot...")
        asyncio.create_task(self.bot.start(discord_token))

        # Wait for bot to be ready
        while not self.bot._connected:
            await asyncio.sleep(1)

        logger.info("Bot connected, starting file watcher loop...")

        # Main loop
        while self.running:
            try:
                await self.check_cycle()

            except Exception as e:
                logger.error(f"Error in check cycle: {e}", exc_info=True)

            # Sleep until next check
            logger.debug(f"Sleeping for {self.check_interval} seconds...")
            await asyncio.sleep(self.check_interval)

    async def check_cycle(self):
        """Execute one check cycle."""
        now = datetime.now(timezone.utc)
        logger.info("Starting check cycle...")

        # Check if in quiet hours
        in_quiet_hours = self.file_watcher.is_quiet_hours()
        was_in_quiet_hours = self.state_manager.was_in_quiet_hours()

        # Update quiet hours status in state
        self.state_manager.set_quiet_hours_status(in_quiet_hours)

        if in_quiet_hours:
            logger.info("Currently in quiet hours, skipping check")
            return

        # Scan folder for files
        current_files = self.file_watcher.scan_folder()
        tracked_files = self.state_manager.get_all_files()

        # Detect changes
        new_files, modified_files, deleted_files = self.file_watcher.detect_changes(
            current_files,
            tracked_files,
        )

        # Process deleted files
        if deleted_files:
            logger.info(f"Processing {len(deleted_files)} deleted files")
            await self.bot.send_deleted_files_notification(deleted_files)

            # Remove deleted files from state
            for file_data in deleted_files:
                self.state_manager.remove_file_state(file_data['path'])
                self.state_manager.remove_pending_file(file_data['path'])

        # Process new and modified files
        await self.process_files(new_files, modified_files)

        # Send summary if coming out of quiet hours and found files
        if was_in_quiet_hours and not in_quiet_hours:
            logger.info("Exiting quiet hours, checking for summary notification...")
            # Note: Since we skip checks during quiet hours, any files that were
            # added during quiet hours would be discovered now and handled
            # through the normal new/modified file flow above.

        # Update last check timestamp
        self.state_manager.update_last_check(now)
        self.state_manager.save_state()

        logger.info("Check cycle completed")

    async def process_files(self, new_files: list, modified_files: list):
        """
        Process new and modified files for stability and send notifications.

        Args:
            new_files: List of new file metadata
            modified_files: List of modified file metadata
        """
        all_files = new_files + modified_files
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
                # File is already being tracked for growth
                previous_size = pending_file['size']
                is_stable = self.file_watcher.check_file_stability(path, size, previous_size)

                if is_stable:
                    stable_result = self.state_manager.update_pending_file_stability(path, True)
                    if stable_result:
                        # File is now stable
                        is_stable = True
                        self.state_manager.remove_pending_file(path)
                        logger.info(f"File {path} is now stable after growth")
                    else:
                        pending_count += 1
                else:
                    # Still growing, reset counter
                    self.state_manager.update_pending_file_stability(path, False)
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
            logger.info(f"Sending notifications for {len(stable_files)} stable file(s)")
            await self.bot.send_new_files_notification(stable_files)

        if pending_count > 0:
            logger.info(f"Tracking {pending_count} file(s) still growing")

    def stop(self):
        """Stop the service gracefully."""
        logger.info("Stopping service...")
        self.running = False


def signal_handler(signum, frame):
    """Handle shutdown signals."""
    logger.info(f"Received signal {signum}, shutting down...")
    sys.exit(0)


def main():
    """Main entry point."""
    # Setup signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("Starting Discord File Watcher Service...")

    try:
        service = FileWatcherService()
        discord_token = service.setup()

        # Run the service
        asyncio.run(service.run(discord_token))

    except Exception as e:
        logger.error(f"Service failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
