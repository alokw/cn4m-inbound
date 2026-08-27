"""Discord webhook integration for sending notifications."""

import asyncio
import logging
from typing import Dict, List, Optional

import aiohttp

from src.utils import format_size

logger = logging.getLogger(__name__)

# Discord rejects message content longer than 2000 characters
MAX_MESSAGE_LENGTH = 2000

# Number of attempts per message before giving up
MAX_RETRIES = 3


class DiscordWebhookNotifier:
    """Sends file watcher notifications through a Discord webhook."""

    def __init__(self, webhook_url: str):
        """
        Initialize the webhook notifier.

        Args:
            webhook_url: Discord webhook URL for notifications
        """
        self.webhook_url = webhook_url
        self._session: Optional[aiohttp.ClientSession] = None

    async def connect(self):
        """Create the HTTP session used for webhook requests."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
            logger.info("Discord webhook session ready")

    async def close(self):
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            logger.info("Discord webhook session closed")
        self._session = None

    async def send_notification(self, message: str) -> bool:
        """
        Send a notification message.

        Args:
            message: Message to send

        Returns:
            True if message sent successfully, False otherwise
        """
        if self._session is None or self._session.closed:
            logger.warning("Webhook session not open, cannot send notification")
            return False

        payload = {
            'content': message,
            # Never let file names ping @everyone, @here, or roles
            'allowed_mentions': {'parse': []},
        }

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with self._session.post(self.webhook_url, json=payload) as response:
                    if response.status == 429:
                        # Rate limited, wait for the window Discord asks for
                        retry_after = await self._get_retry_after(response)
                        logger.warning(
                            f"Rate limited by Discord, retrying in {retry_after:.1f}s"
                        )
                        await asyncio.sleep(retry_after)
                        continue

                    if 200 <= response.status < 300:
                        logger.info("Notification sent via webhook")
                        return True

                    body = await response.text()
                    logger.error(
                        f"Webhook returned HTTP {response.status}: {body[:500]}"
                    )

                    if response.status < 500:
                        # Client errors (bad URL, deleted webhook) will not fix themselves
                        return False

            except asyncio.TimeoutError:
                logger.error(f"Webhook request timed out (attempt {attempt}/{MAX_RETRIES})")
            except aiohttp.ClientError as e:
                logger.error(f"Webhook request failed (attempt {attempt}/{MAX_RETRIES}): {e}")

            if attempt < MAX_RETRIES:
                await asyncio.sleep(2 ** attempt)

        logger.error("Failed to send notification after retries")
        return False

    @staticmethod
    async def _get_retry_after(response: aiohttp.ClientResponse) -> float:
        """
        Read the retry delay from a rate limited response.

        Args:
            response: The 429 response from Discord

        Returns:
            Delay in seconds before retrying
        """
        try:
            data = await response.json()
            return float(data.get('retry_after', 1.0))
        except Exception:
            try:
                return float(response.headers.get('Retry-After', 1.0))
            except (TypeError, ValueError):
                return 1.0

    async def _send_chunked(self, parts: List[str]) -> bool:
        """
        Send a list of lines as one or more messages, each within Discord's 2000 char limit.

        Args:
            parts: Lines to send in order

        Returns:
            True if all messages sent successfully
        """
        chunks = []
        current: List[str] = []
        current_len = 0

        for part in parts:
            addition = len(part) + (1 if current else 0)  # +1 for newline
            if current and current_len + addition > MAX_MESSAGE_LENGTH:
                chunks.append("\n".join(current))
                current = [part]
                current_len = len(part)
            else:
                current.append(part)
                current_len += addition

        if current:
            chunks.append("\n".join(current))

        success = True
        for chunk in chunks:
            if not await self.send_notification(chunk):
                success = False
        return success

    async def send_new_files_notification(
        self,
        files: List[Dict[str, any]],
        timezone_str: str = 'UTC'
    ) -> bool:
        """
        Send notification for new files.

        Args:
            files: List of file dictionaries with metadata
            timezone_str: Timezone for timestamps

        Returns:
            True if notification sent successfully
        """
        if not files:
            return True

        count = len(files)
        label = "file" if count == 1 else "files"
        message_parts = [
            f"📁 **{count} new {label} discovered:**"
        ]

        for file_data in sorted(files, key=lambda f: f['relative_path']):
            path = file_data['relative_path']
            size = format_size(file_data['size'])
            message_parts.append(f"• `{path}` ({size})")

        return await self._send_chunked(message_parts)

    async def send_deleted_files_notification(
        self,
        files: List[Dict[str, any]],
    ) -> bool:
        """
        Send notification for deleted or moved files.

        Args:
            files: List of file dictionaries with metadata

        Returns:
            True if notification sent successfully
        """
        if not files:
            return True

        count = len(files)
        label = "file" if count == 1 else "files"
        message_parts = [
            f"🗑️ **{count} {label} deleted or moved:**"
        ]

        for file_data in sorted(files, key=lambda f: f['path']):
            path = file_data['path']
            size = format_size(file_data['size'])
            message_parts.append(f"• `{path}` ({size})")

        return await self._send_chunked(message_parts)

    async def send_summary_notification(
        self,
        new_files: List[Dict[str, any]],
        deleted_files: List[Dict[str, any]],
    ) -> bool:
        """
        Send summary notification after quiet hours.

        Args:
            new_files: List of new file dictionaries
            deleted_files: List of deleted file dictionaries

        Returns:
            True if notification sent successfully
        """
        if not new_files and not deleted_files:
            return True

        message_parts = ["🌅 **Summary after quiet hours:**", ""]

        if new_files:
            message_parts.append(f"Discovered {len(new_files)} new file(s):")
            for file_data in new_files:
                path = file_data['relative_path']
                size = format_size(file_data['size'])
                mime_type = file_data['mime_type']
                message_parts.append(f"• `{path}` ({size}, {mime_type})")
            message_parts.append("")

        if deleted_files:
            message_parts.append(f"{len(deleted_files)} file(s) were deleted or moved:")
            for file_data in deleted_files:
                path = file_data['path']
                message_parts.append(f"• `{path}`")

        return await self._send_chunked(message_parts)

    async def send_error_notification(self, error_message: str) -> bool:
        """
        Send error notification.

        Args:
            error_message: Error message to send

        Returns:
            True if notification sent successfully
        """
        message = f"⚠️ **Error:** {error_message}"
        return await self.send_notification(message)
