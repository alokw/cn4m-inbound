"""State manager for tracking file metadata in JSON format."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class StateManager:
    """Manages persistent state of tracked files."""

    STATE_VERSION = 1

    def __init__(self, state_file: str):
        """
        Initialize state manager.

        Args:
            state_file: Path to JSON state file
        """
        self.state_file = Path(state_file)
        self.state = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        """
        Load state from JSON file.

        Returns:
            State dictionary with default structure
        """
        if not self.state_file.exists():
            logger.info(f"State file not found, creating new state: {self.state_file}")
            return self._create_empty_state()

        try:
            with open(self.state_file, 'r', encoding='utf-8') as f:
                state = json.load(f)

            # Validate and migrate state if needed
            if state.get('version') != self.STATE_VERSION:
                logger.warning(f"State version mismatch, resetting state")
                return self._create_empty_state()

            logger.info(f"Loaded state from {self.state_file}")
            return state

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse state file: {e}")
            logger.info("Creating new state file")
            return self._create_empty_state()

    def _create_empty_state(self) -> Dict[str, Any]:
        """
        Create empty state structure.

        Returns:
            Empty state dictionary
        """
        return {
            'version': self.STATE_VERSION,
            'last_check': None,
            'files': {},
            'pending_files': {},
            'last_quiet_hours_check': None,
            'in_quiet_hours': False,
        }

    def save_state(self) -> None:
        """Save current state to JSON file."""
        try:
            # Ensure parent directory exists
            self.state_file.parent.mkdir(parents=True, exist_ok=True)

            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, indent=2, default=str)

            logger.debug(f"Saved state to {self.state_file}")

        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def get_file_state(self, file_path: str) -> Optional[Dict[str, Any]]:
        """
        Get state for a specific file.

        Args:
            file_path: Relative path to file

        Returns:
            File state dictionary or None if not found
        """
        return self.state['files'].get(file_path)

    def update_file_state(
        self,
        file_path: str,
        size: int,
        mime_type: str,
        modified_time: datetime,
        status: str = 'stable',
    ) -> None:
        """
        Update or create file state.

        Args:
            file_path: Relative path to file
            size: File size in bytes
            mime_type: MIME type
            modified_time: File modification time
            status: File status ('stable', 'pending', etc.)
        """
        now = datetime.now(timezone.utc)

        existing_state = self.state['files'].get(file_path)

        if existing_state:
            # Update existing file state
            existing_state.update({
                'size': size,
                'mime_type': mime_type,
                'modified_time': modified_time.isoformat(),
                'last_seen': now.isoformat(),
                'status': status,
            })
        else:
            # Create new file state
            self.state['files'][file_path] = {
                'size': size,
                'mime_type': mime_type,
                'modified_time': modified_time.isoformat(),
                'first_seen': now.isoformat(),
                'last_seen': now.isoformat(),
                'stable_checks': 0,
                'status': status,
            }

    def remove_file_state(self, file_path: str) -> bool:
        """
        Remove file from state.

        Args:
            file_path: Relative path to file

        Returns:
            True if file was removed, False if not found
        """
        if file_path in self.state['files']:
            del self.state['files'][file_path]
            logger.debug(f"Removed file from state: {file_path}")
            return True

        # Also check pending files
        if file_path in self.state['pending_files']:
            del self.state['pending_files'][file_path]
            logger.debug(f"Removed pending file from state: {file_path}")
            return True

        return False

    def get_all_files(self) -> Dict[str, Dict[str, Any]]:
        """
        Get all tracked files.

        Returns:
            Dictionary of all files with their states
        """
        return self.state['files'].copy()

    def get_pending_files(self) -> Dict[str, Dict[str, Any]]:
        """
        Get all pending files (still growing).

        Returns:
            Dictionary of pending files with their states
        """
        return self.state['pending_files'].copy()

    def add_pending_file(
        self,
        file_path: str,
        size: int,
        first_seen: datetime,
    ) -> None:
        """
        Add or update a pending file (still growing).

        Args:
            file_path: Relative path to file
            size: Current file size
            first_seen: When file was first seen
        """
        if file_path in self.state['pending_files']:
            # Update existing pending file
            pending = self.state['pending_files'][file_path]
            pending['size'] = size
            pending['previous_sizes'].append(size)
            pending['stable_checks'] = 0
        else:
            # Add new pending file
            self.state['pending_files'][file_path] = {
                'size': size,
                'previous_sizes': [size],
                'first_seen': first_seen.isoformat(),
                'stable_checks': 0,
            }

    def update_pending_file_stability(self, file_path: str, stable: bool) -> Optional[Dict[str, Any]]:
        """
        Update stability check count for pending file.

        Args:
            file_path: Relative path to file
            stable: Whether file size is stable (unchanged)

        Returns:
            Pending file state if stability threshold reached, None otherwise
        """
        if file_path not in self.state['pending_files']:
            return None

        pending = self.state['pending_files'][file_path]

        if stable:
            pending['stable_checks'] += 1
        else:
            pending['stable_checks'] = 0

        # Return file state if stable (caller should move to main files dict)
        return pending if pending['stable_checks'] >= 3 else None

    def remove_pending_file(self, file_path: str) -> bool:
        """
        Remove file from pending.

        Args:
            file_path: Relative path to file

        Returns:
            True if file was removed, False if not found
        """
        if file_path in self.state['pending_files']:
            del self.state['pending_files'][file_path]
            return True
        return False

    def update_last_check(self, timestamp: datetime) -> None:
        """
        Update last check timestamp.

        Args:
            timestamp: Timestamp of last check
        """
        self.state['last_check'] = timestamp.isoformat()

    def set_quiet_hours_status(self, in_quiet_hours: bool) -> None:
        """
        Set quiet hours status and update timestamp.

        Args:
            in_quiet_hours: Whether currently in quiet hours
        """
        self.state['in_quiet_hours'] = in_quiet_hours
        self.state['last_quiet_hours_check'] = datetime.now(timezone.utc).isoformat()

    def was_in_quiet_hours(self) -> bool:
        """
        Check if last check was during quiet hours.

        Returns:
            True if last check was during quiet hours
        """
        return self.state.get('in_quiet_hours', False)
