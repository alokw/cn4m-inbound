"""Discord bot integration for sending notifications."""

import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional

import discord
from discord.ext import commands

from src.utils import format_size, format_timestamp

logger = logging.getLogger(__name__)


class DiscordFileWatcherBot(commands.Bot):
    """Discord bot for file watcher notifications."""

    def __init__(self, channel_id: int):
        """
        Initialize Discord bot.

        Args:
            channel_id: Discord channel ID for notifications
        """
        intents = discord.Intents.default()
        intents.message_content = False
        super().__init__(command_prefix='!', intents=intents)

        self.channel_id = channel_id
        self._connected = False

    async def on_ready(self):
        """Called when bot is ready."""
        logger.info(f"Bot connected as {self.user}")
        self._connected = True

    async def send_notification(self, message: str) -> bool:
        """
        Send a notification message.

        Args:
            message: Message to send

        Returns:
            True if message sent successfully, False otherwise
        """
        if not self._connected:
            logger.warning("Bot not connected, cannot send notification")
            return False

        try:
            channel = self.get_channel(self.channel_id)
            if not channel:
                logger.error(f"Channel {self.channel_id} not found")
                return False

            await channel.send(message)
            logger.info(f"Notification sent to channel {self.channel_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to send notification: {e}")
            return False

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

        # Group files by modified time to show common timestamp
        # Use the most recent modified time as reference
        most_recent = max(
            datetime.fromisoformat(f['modified_time'])
            for f in files
        )
        processed_time = datetime.utcnow()

        message_parts = [
            "📁 **New file(s) discovered:**",
            ""
        ]

        for file_data in files:
            path = file_data['path']
            size = format_size(file_data['size'])
            mime_type = file_data['mime_type']
            message_parts.append(f"• `{path}` ({size}, {mime_type})")

        message_parts.append("")
        message_parts.append(f"Modified: {format_timestamp(most_recent)}")
        message_parts.append(f"Processed: {format_timestamp(processed_time)}")

        message = "\n".join(message_parts)
        return await self.send_notification(message)

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

        message_parts = [
            "🗑️ **File(s) deleted or moved:**",
            ""
        ]

        for file_data in files:
            path = file_data['path']
            size = format_size(file_data['size'])
            mime_type = file_data.get('mime_type', 'unknown')
            last_seen = format_timestamp(datetime.fromisoformat(file_data['last_seen']))

            message_parts.append(f"• `{path}` ({size}, {mime_type})")
            message_parts.append(f"  Last seen: {last_seen}")

        message = "\n".join(message_parts)
        return await self.send_notification(message)

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
                path = file_data['path']
                size = format_size(file_data['size'])
                mime_type = file_data['mime_type']
                message_parts.append(f"• `{path}` ({size}, {mime_type})")
            message_parts.append("")

        if deleted_files:
            message_parts.append(f"{len(deleted_files)} file(s) were deleted or moved:")
            for file_data in deleted_files:
                path = file_data['path']
                message_parts.append(f"• `{path}`")

        message = "\n".join(message_parts)
        return await self.send_notification(message)

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


class DiscordNotifier:
    """Sync wrapper for async Discord bot."""

    def __init__(self, token: str, channel_id: int):
        """
        Initialize Discord notifier.

        Args:
            token: Discord bot token
            channel_id: Discord channel ID for notifications
        """
        self.token = token
        self.channel_id = channel_id
        self.bot = None
        self.loop = None

    def start(self):
        """Start the Discord bot (blocking)."""
        self.bot = DiscordFileWatcherBot(self.channel_id)
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        try:
            self.loop.run_until_complete(self.bot.start(self.token))
        except KeyboardInterrupt:
            pass
        finally:
            self.loop.close()

    async def _run_async(self, coro):
        """Run async coroutine in the bot's event loop."""
        if not self.loop or self.loop.is_closed():
            return False

        return self.loop.run_until_complete(coro)

    def send_notification(self, message: str) -> bool:
        """
        Send a notification message (sync wrapper).

        Args:
            message: Message to send

        Returns:
            True if message sent successfully
        """
        if not self.bot:
            return False

        return asyncio.run_coroutine_threadsafe(
            self.bot.send_notification(message),
            self.bot.loop
        ).result()

    def send_new_files_notification(self, files: List[Dict[str, any]]) -> bool:
        """
        Send notification for new files (sync wrapper).

        Args:
            files: List of file dictionaries

        Returns:
            True if notification sent successfully
        """
        if not self.bot:
            return False

        return asyncio.run_coroutine_threadsafe(
            self.bot.send_new_files_notification(files),
            self.bot.loop
        ).result()

    def send_deleted_files_notification(self, files: List[Dict[str, any]]) -> bool:
        """
        Send notification for deleted files (sync wrapper).

        Args:
            files: List of file dictionaries

        Returns:
            True if notification sent successfully
        """
        if not self.bot:
            return False

        return asyncio.run_coroutine_threadsafe(
            self.bot.send_deleted_files_notification(files),
            self.bot.loop
        ).result()

    def send_summary_notification(
        self,
        new_files: List[Dict[str, any]],
        deleted_files: List[Dict[str, any]],
    ) -> bool:
        """
        Send summary notification (sync wrapper).

        Args:
            new_files: List of new file dictionaries
            deleted_files: List of deleted file dictionaries

        Returns:
            True if notification sent successfully
        """
        if not self.bot:
            return False

        return asyncio.run_coroutine_threadsafe(
            self.bot.send_summary_notification(new_files, deleted_files),
            self.bot.loop
        ).result()

    def stop(self):
        """Stop the Discord bot."""
        if self.bot:
            asyncio.run_coroutine_threadsafe(
                self.bot.close(),
                self.bot.loop
            ).result()
