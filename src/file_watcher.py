"""File watching logic for scanning and detecting changes."""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pytz

from src.utils import get_env_bool, get_env_int, get_mime_type, get_relative_path

logger = logging.getLogger(__name__)


class FileWatcher:
    """Watches a folder for file changes."""

    def __init__(
        self,
        watch_folder: str,
        stability_checks: int = 3,
        quiet_hours_enabled: bool = False,
        quiet_hours_start: int = 22,
        quiet_hours_end: int = 8,
        timezone: str = 'UTC',
        min_file_size: int = 0,
    ):
        """
        Initialize file watcher.

        Args:
            watch_folder: Path to folder to watch
            stability_checks: Number of stable size checks required
            quiet_hours_enabled: Whether quiet hours are enabled
            quiet_hours_start: Hour when quiet hours start (0-23)
            quiet_hours_end: Hour when quiet hours end (0-23)
            timezone: Timezone for quiet hours
            min_file_size: Minimum file size to check for growth (0 = all files)
        """
        self.watch_folder = Path(watch_folder)
        self.stability_checks = stability_checks
        self.quiet_hours_enabled = quiet_hours_enabled
        self.quiet_hours_start = quiet_hours_start
        self.quiet_hours_end = quiet_hours_end
        self.timezone = pytz.timezone(timezone)
        self.min_file_size = min_file_size

    def scan_folder(self) -> Dict[str, Dict[str, any]]:
        """
        Recursively scan watch folder and return all files with metadata.

        Returns:
            Dictionary mapping relative paths to file metadata
        """
        files = {}

        if not self.watch_folder.exists():
            logger.error(f"Watch folder does not exist: {self.watch_folder}")
            return files

        try:
            for file_path in self.watch_folder.rglob('*'):
                # Skip directories
                if file_path.is_dir():
                    continue

                # Skip symlinks
                if file_path.is_symlink():
                    continue

                try:
                    metadata = self._get_file_metadata(file_path)
                    relative_path = metadata['relative_path']
                    files[relative_path] = metadata

                except Exception as e:
                    logger.warning(f"Failed to get metadata for {file_path}: {e}")
                    continue

            logger.info(f"Scanned {len(files)} files in {self.watch_folder}")
            return files

        except Exception as e:
            logger.error(f"Failed to scan folder: {e}")
            return {}

    def _get_file_metadata(self, file_path: Path) -> Dict[str, any]:
        """
        Get metadata for a single file.

        Args:
            file_path: Absolute path to file

        Returns:
            Dictionary with file metadata
        """
        stat = file_path.stat()

        return {
            'absolute_path': str(file_path),
            'relative_path': get_relative_path(str(file_path), str(self.watch_folder)),
            'size': stat.st_size,
            'modified_time': datetime.fromtimestamp(stat.st_mtime).isoformat(),
            'mime_type': get_mime_type(str(file_path)),
        }

    def detect_changes(
        self,
        current_files: Dict[str, Dict[str, any]],
        tracked_files: Dict[str, Dict[str, any]],
    ) -> Tuple[List[Dict[str, any]], List[Dict[str, any]], List[Dict[str, any]]]:
        """
        Detect new, modified, and deleted files.

        Args:
            current_files: Currently present files from scan
            tracked_files: Files in state from previous checks

        Returns:
            Tuple of (new_files, modified_files, deleted_files)
        """
        current_paths: Set[str] = set(current_files.keys())
        tracked_paths: Set[str] = set(tracked_files.keys())

        new_paths = current_paths - tracked_paths
        deleted_paths = tracked_paths - current_paths

        # For modified files, check if size or modification time changed
        modified_paths = []
        for path in current_paths & tracked_paths:
            current = current_files[path]
            tracked = tracked_files[path]

            if (current['size'] != tracked['size'] or
                current['modified_time'] != tracked['modified_time']):
                modified_paths.append(path)

        new_files = [current_files[p] for p in new_paths]
        modified_files = [current_files[p] for p in modified_paths]
        deleted_files = [
            {**tracked_files[p], 'path': p}
            for p in deleted_paths
        ]

        logger.info(
            f"Detected changes: {len(new_files)} new, "
            f"{len(modified_files)} modified, {len(deleted_files)} deleted"
        )

        return new_files, modified_files, deleted_files

    def check_file_stability(
        self,
        file_path: str,
        current_size: int,
        previous_size: Optional[int] = None,
    ) -> bool:
        """
        Check if file size is stable (not growing).

        Args:
            file_path: Relative path to file
            current_size: Current file size
            previous_size: Previous file size (if available)

        Returns:
            True if file size is stable, False if still growing
        """
        # If no previous size, we can't determine stability yet
        if previous_size is None:
            logger.debug(f"No previous size for {file_path}, assuming unstable")
            return False

        # Check if size changed
        is_stable = current_size == previous_size

        if not is_stable:
            logger.debug(
                f"File {file_path} is still growing: "
                f"{previous_size} -> {current_size}"
            )

        return is_stable

    def is_quiet_hours(self) -> bool:
        """
        Check if current time is within quiet hours.

        Returns:
            True if currently in quiet hours
        """
        if not self.quiet_hours_enabled:
            return False

        try:
            # Get current time in configured timezone
            now = datetime.now(self.timezone)
            current_hour = now.hour

            # Handle case where quiet hours span midnight (e.g., 22:00 to 08:00)
            if self.quiet_hours_start > self.quiet_hours_end:
                # Quiet hours span midnight
                in_quiet_hours = (
                    current_hour >= self.quiet_hours_start or
                    current_hour < self.quiet_hours_end
                )
            else:
                # Normal range (e.g., 01:00 to 06:00)
                in_quiet_hours = (
                    self.quiet_hours_start <= current_hour < self.quiet_hours_end
                )

            if in_quiet_hours:
                logger.debug(f"Currently in quiet hours ({current_hour}:00)")

            return in_quiet_hours

        except Exception as e:
            logger.error(f"Failed to check quiet hours: {e}")
            return False

    def should_check_for_growth(self, file_size: int) -> bool:
        """
        Determine if file should be checked for growth based on size.

        Args:
            file_size: File size in bytes

        Returns:
            True if file should be checked for growth
        """
        return file_size >= self.min_file_size


def create_from_env() -> FileWatcher:
    """
    Create FileWatcher instance from environment variables.

    Returns:
        Configured FileWatcher instance

    Raises:
        ValueError: If required environment variables are missing
    """
    watch_folder = os.getenv('WATCH_FOLDER')
    if not watch_folder:
        raise ValueError("WATCH_FOLDER environment variable is required")

    stability_checks = get_env_int('STABILITY_CHECKS', 3)
    quiet_hours_enabled = get_env_bool('QUIET_HOURS_ENABLED', False)
    quiet_hours_start = get_env_int('QUIET_HOURS_START', 22)
    quiet_hours_end = get_env_int('QUIET_HOURS_END', 8)
    timezone = os.getenv('TIMEZONE', 'UTC')
    min_file_size = get_env_int('MIN_FILE_SIZE', 0)

    return FileWatcher(
        watch_folder=watch_folder,
        stability_checks=stability_checks,
        quiet_hours_enabled=quiet_hours_enabled,
        quiet_hours_start=quiet_hours_start,
        quiet_hours_end=quiet_hours_end,
        timezone=timezone,
        min_file_size=min_file_size,
    )
