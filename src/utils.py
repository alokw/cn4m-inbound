"""Utility functions for the file watcher service."""

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import magic
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


def parse_interval(interval_str: str) -> int:
    """
    Parse interval string (e.g., '30s', '5m', '1h', '1d') to seconds.

    Args:
        interval_str: Interval string with suffix (s/m/h/d)

    Returns:
        int: Interval in seconds

    Raises:
        ValueError: If interval string is invalid
    """
    if not interval_str:
        raise ValueError("Interval string cannot be empty")

    interval_str = interval_str.strip().lower()
    match = re.match(r'^(\d+(?:\.\d+)?)\s*([smhd])$', interval_str)

    if not match:
        raise ValueError(
            f"Invalid interval format: {interval_str}. "
            "Expected format: <number><unit> (e.g., '30s', '5m', '1h', '1d')"
        )

    value = float(match.group(1))
    unit = match.group(2)

    multipliers = {
        's': 1,
        'm': 60,
        'h': 60 * 60,
        'd': 24 * 60 * 60,
    }

    return int(value * multipliers[unit])


def get_mime_type(file_path: str) -> str:
    """
    Get MIME type of a file using python-magic.

    Args:
        file_path: Path to the file

    Returns:
        str: MIME type (e.g., 'image/jpeg')
    """
    try:
        mime = magic.Magic(mime=True)
        return mime.from_file(file_path)
    except Exception:
        # Fallback to octet-stream if detection fails
        return 'application/octet-stream'


def get_relative_path(file_path: str, base_folder: str) -> str:
    """
    Get relative path from base folder.

    Args:
        file_path: Absolute path to file
        base_folder: Base folder path

    Returns:
        str: Relative path from base folder
    """
    try:
        return str(Path(file_path).relative_to(Path(base_folder)))
    except ValueError:
        # If file is not under base folder, return absolute path
        return file_path


def format_timestamp(timestamp: Optional[datetime], format_str: str = '%Y-%m-%d %H:%M:%S %Z') -> str:
    """
    Format datetime object to string.

    Args:
        timestamp: Datetime object (or None)
        format_str: strftime format string

    Returns:
        str: Formatted timestamp or 'Unknown' if None
    """
    if timestamp is None:
        return 'Unknown'
    return timestamp.strftime(format_str)


def format_size(size_bytes: int) -> str:
    """
    Format byte size to human-readable string.

    Args:
        size_bytes: Size in bytes

    Returns:
        str: Human-readable size (e.g., '1.5 GB', '500 MB')
    """
    if size_bytes == 0:
        return '0 B'

    units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
    unit_index = 0
    size = float(size_bytes)

    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1

    if unit_index == 0:
        return f'{int(size)} {units[unit_index]}'
    else:
        return f'{size:.1f} {units[unit_index]}'


def get_env_var(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    """
    Get environment variable with optional default and required validation.

    Args:
        name: Environment variable name
        default: Default value if not set
        required: If True, raise error when variable is not set

    Returns:
        Environment variable value or default

    Raises:
        ValueError: If required variable is not set
    """
    value = os.getenv(name, default)

    if required and not value:
        raise ValueError(f"Required environment variable '{name}' is not set")

    return value


def get_env_int(name: str, default: int) -> int:
    """
    Get integer environment variable with default.

    Args:
        name: Environment variable name
        default: Default value if not set

    Returns:
        int: Environment variable value as integer

    Raises:
        ValueError: If value is not a valid integer
    """
    value = os.getenv(name, str(default))

    try:
        return int(value)
    except ValueError:
        raise ValueError(f"Environment variable '{name}' must be an integer, got: {value}")


def get_env_bool(name: str, default: bool) -> bool:
    """
    Get boolean environment variable with default.

    Args:
        name: Environment variable name
        default: Default value if not set

    Returns:
        bool: Environment variable value as boolean
    """
    value = os.getenv(name, str(default)).lower()

    if value in ('true', '1', 'yes', 'on'):
        return True
    elif value in ('false', '0', 'no', 'off'):
        return False
    else:
        return default
