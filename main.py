# @author: Panado (yesdotcom), 2026

import asyncio
import io
import json
import logging
import os
import re
import sys

import discord
import sentry_sdk
from discord.ext import commands
from dotenv import load_dotenv
from sentry_sdk.integrations.logging import EventHandler
from tortoise import Tortoise

from utils.discord_utils import (
    DISCORD_EMBED_DESCRIPTION_LIMIT,
    DISCORD_EMBED_FIELD_NAME_LIMIT,
    DISCORD_EMBED_FIELD_VALUE_LIMIT,
    DISCORD_EMBED_TITLE_LIMIT,
    DISCORD_MESSAGE_LIMIT,
    CloseTicketButton,
    DiscordCommands,
    TicketResponseTimeoutHandler,
    TicketSupportEmbedManager,
    truncate_text,
)
from utils.sql_utils import DatabaseOperations, staff_response_count

# Load environment variables
load_dotenv()


sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN"),
    send_default_pii=True,
    max_request_body_size="always",
    traces_sample_rate=1.0,
)

# Create the Sentry logging handler
sentry_handler = EventHandler(level=logging.ERROR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        sentry_handler,  # <--- sends errors from logger to Sentry
    ],
)
logger = logging.getLogger("main.py")


# Custom Bot class to initialize Tortoise ORM
class TicketBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tortoise_initialized = False

    async def setup_hook(self):
        """Called before the bot connects to Discord"""
        if not self.tortoise_initialized:
            try:
                await init_tortoise()
                self.tortoise_initialized = True
                logger.info("✅ Tortoise ORM initialized successfully")
            except Exception as e:
                logger.error(f"❌ Failed to initialize Tortoise ORM: {e}")
                self.tortoise_initialized = False


# Initialize Discord bot
intents = discord.Intents.all()
bot = TicketBot(command_prefix="", intents=intents, help_command=None)


@bot.event
async def on_ready():
    await bot.wait_until_ready()

    try:
        # Tortoise ORM should already be initialized in setup_hook
        if not bot.tortoise_initialized:
            logger.error("❌ Tortoise ORM was not initialized in setup_hook")
            return

        # Setup persistent views and ticket embed manager
        ticket_embed_manager = TicketSupportEmbedManager(bot)

        # Add the DiscordCommands cog
        await bot.add_cog(DiscordCommands(bot))
        logger.info("Added DiscordCommands cog")

        # Add the TicketResponseTimeoutHandler cog and start the loop task
        timeout_handler = TicketResponseTimeoutHandler(bot)
        await bot.add_cog(timeout_handler)
        timeout_handler.check_awaiting_response_tickets.start()
        logger.info("Added TicketResponseTimeoutHandler cog and started loop task")

        if not ticket_embed_manager.config:
            logger.error("Failed to load config")
            return

        # Get main guild id from config
        main_guild_id = ticket_embed_manager.config.get("main_guild_id")
        if not main_guild_id:
            logger.error("main_guild_id is not set in config.json")
            return

        # Copy commands from the cog to the guild tree
        guild_obj = discord.Object(id=main_guild_id)
        bot.tree.copy_global_to(guild=guild_obj)

        synced = await bot.tree.sync(guild=guild_obj)
        logger.info(f"✅ Synced {len(synced)} command(s) to guild {main_guild_id}")

        # Debug: Print command names
        for cmd in synced:
            logger.info(f"🔧 Command synced: {cmd.name}")

        # Setup persistent view
        persistent_view = ticket_embed_manager.create_persistent_view()
        bot.add_view(persistent_view)
        logger.info("Added persistent view for ticket buttons")

        # Add persistent view for close ticket buttons
        close_ticket_view = discord.ui.View(timeout=None)
        close_button = CloseTicketButton(bot=bot)
        close_ticket_view.add_item(close_button)
        bot.add_view(close_ticket_view)
        logger.info("Added persistent view for close ticket buttons")

        # Get ticket support channels from config
        if ticket_embed_manager.config:
            ticket_channels = ticket_embed_manager.config.get(
                "ticket_support_channels", []
            )

            for channel_id in ticket_channels:
                channel = bot.get_channel(channel_id)
                if channel:
                    # Check if embed already exists (check last message)
                    messages = [msg async for msg in channel.history(limit=1)]

                    # Only create if channel is empty or last message isn't from the bot
                    if not messages or messages[0].author.id != bot.user.id:
                        await ticket_embed_manager.create_ticket_support_embed(channel)
                        logger.info(
                            f"Created ticket support embed in channel {channel_id}"
                        )
                    else:
                        logger.info(
                            f"ℹTicket support embed already exists in channel {channel_id}"
                        )
                else:
                    logger.error(f"Could not find channel with ID {channel_id}")

    except Exception as e:
        logger.error(f"Failed to sync commands or add cog: {e}")


def is_ticket_channel(channel_name: str) -> bool:
    """Check if a channel is a ticket channel based on its name format."""
    if "-" not in channel_name:
        return False

    try:
        # Ticket channels end with user ID (format: emoji-name-guild-userid)
        int(channel_name.split("-")[-1])
        return True
    except (ValueError, IndexError):
        return False


async def check_and_reply_faq(message: discord.Message, config: dict) -> bool:
    """
    Check if message matches any FAQ patterns and reply with the answer.
    Returns True if an FAQ was matched, False otherwise.
    """
    faq_data = config.get("faq", {})
    questions = faq_data.get("questions", [])

    if not questions:
        return False

    # Normalize message content for matching
    message_lower = message.content.lower()

    # Check each FAQ question pattern
    for faq in questions:
        question_patterns = faq.get("question", "").lower()
        answer = faq.get("answer", "")

        if not question_patterns or not answer:
            continue

        # Split patterns by comma for multiple keywords/phrases
        patterns = [pattern.strip() for pattern in question_patterns.split(",")]

        # Check if any pattern is in the message (using word boundaries for exact matching)
        for pattern in patterns:
            # Escape special regex characters and add word boundaries
            escaped_pattern = re.escape(pattern)
            # Use \b for word boundaries to match whole words/phrases only
            regex_pattern = r"\b" + escaped_pattern + r"\b"
            if re.search(regex_pattern, message_lower):
                try:
                    await message.reply(answer, mention_author=False)
                    logger.info(
                        f"Replied to FAQ question from {message.author.name}: {pattern}"
                    )
                    return True
                except Exception as e:
                    logger.error(f"Failed to send FAQ reply: {e}")
                    return False

    return False


@bot.event
async def on_message(message: discord.Message):
    """Handle message forwarding between tickets and DMs"""
    # Ignore bot's own messages
    if message.author.bot:
        return

    # Process commands first
    await bot.process_commands(message)

    # React to greetings with a wave emoji
    if message.content.lower().strip() in ["hi", "hello", "good morning!"]:
        try:
            await message.add_reaction("👋")
        except Exception as e:
            logger.warning(f"Failed to add reaction to greeting: {e}")

    # Import needed utilities
    from utils.discord_utils import find_user_ticket, load_config, users_in_process

    config = load_config()
    if not config:
        return

    # Check for FAQ matches in guild text channels (not DMs, not tickets)
    if isinstance(message.channel, discord.TextChannel):
        if not is_ticket_channel(message.channel.name):
            # This is a regular guild channel, check for FAQ
            await check_and_reply_faq(message, config)

    # Handle DM messages from users (forward to their ticket)
    if isinstance(message.channel, discord.DMChannel):
        # Don't forward if user is in ticket creation process
        if message.author.id in users_in_process:
            return

        # Find the user's guild (assuming main guild from config)
        main_guild_id = config.get("main_guild_id")
        if not main_guild_id:
            logger.error("main_guild_id not set in config")
            return

        guild = bot.get_guild(main_guild_id)
        if not guild:
            logger.error(f"Could not find guild with ID {main_guild_id}")
            return

        # Find user's most recent ticket
        ticket_channel = await find_user_ticket(guild, message.author.id)
        if not ticket_channel:
            # User doesn't have a ticket
            logger.debug(
                f"User {message.author.name} has no open ticket to forward DM to."
            )
            # respond with no open tickets
            await message.channel.send(
                "❌ You don't have any open tickets. Please create a ticket first."
            )
            return

        # Forward the message to the ticket channel
        try:
            embed = discord.Embed(
                description=truncate_text(
                    message.content, DISCORD_EMBED_DESCRIPTION_LIMIT
                ),
                color=discord.Color.blue(),
            )
            icon_url = (
                message.author.display_avatar.url
                if message.author.display_avatar
                else "https://pub-ac6368b6320d4e8bb06d39c4ace57205.r2.dev/discord-logo-01-discord-logo-11562849833clsolz2mbc-280419404.png"
            )
            embed.set_author(
                name=truncate_text(
                    message.author.global_name, DISCORD_EMBED_TITLE_LIMIT
                ),
                icon_url=icon_url,
            )

            # Handle attachments with security checks
            files = []
            MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB limit
            ALLOWED_EXTENSIONS = {
                # Images
                "png",
                "jpg",
                "jpeg",
                "gif",
                "webp",
                "bmp",
                # Documents
                "pdf",
                "doc",
                "docx",
                "xls",
                "xlsx",
                "txt",
                "csv",
                # Archives
                "zip",
                "rar",
                "7z",
                "tar",
                "gz",
                # Media
                "mp3",
                "mp4",
                "wav",
                "mov",
                "avi",
            }

            if message.attachments:
                for attachment in message.attachments:
                    # Validate file size
                    if attachment.size > MAX_FILE_SIZE:
                        embed.add_field(
                            name=truncate_text(
                                "Attachment (Too Large)", DISCORD_EMBED_FIELD_NAME_LIMIT
                            ),
                            value=truncate_text(
                                f"[{attachment.filename}]({attachment.url}) - File exceeds 25 MB limit",
                                DISCORD_EMBED_FIELD_VALUE_LIMIT,
                            ),
                            inline=False,
                        )
                        logger.warning(
                            f"File {attachment.filename} exceeds size limit: {attachment.size} bytes"
                        )
                        continue

                    # Validate file extension
                    file_ext = (
                        attachment.filename.rsplit(".", 1)[-1].lower()
                        if "." in attachment.filename
                        else ""
                    )
                    if file_ext not in ALLOWED_EXTENSIONS:
                        embed.add_field(
                            name=truncate_text(
                                "Attachment (Blocked)", DISCORD_EMBED_FIELD_NAME_LIMIT
                            ),
                            value=truncate_text(
                                f"[{attachment.filename}]({attachment.url}) - File type not allowed",
                                DISCORD_EMBED_FIELD_VALUE_LIMIT,
                            ),
                            inline=False,
                        )
                        logger.warning(
                            f"File {attachment.filename} has disallowed extension: {file_ext}"
                        )
                        continue

                    embed.add_field(
                        name=truncate_text(
                            "Attachment", DISCORD_EMBED_FIELD_NAME_LIMIT
                        ),
                        value=truncate_text(
                            f"[{attachment.filename}]({attachment.url})",
                            DISCORD_EMBED_FIELD_VALUE_LIMIT,
                        ),
                        inline=False,
                    )
                    # Download and prepare file for forwarding
                    try:
                        file_data = await attachment.read()
                        files.append(
                            discord.File(
                                io.BytesIO(file_data), filename=attachment.filename
                            )
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to download attachment {attachment.filename}: {e}"
                        )

            # Check if ticket is marked as awaiting response and move it back if so
            if ticket_channel.topic and ticket_channel.topic.startswith("{"):
                try:
                    ticket_metadata = json.loads(ticket_channel.topic)
                    if ticket_metadata.get("ticket_config", {}).get(
                        "awaiting_response", False
                    ):
                        # Get ticket type and find the normal category
                        ticket_type = ticket_metadata["ticket_config"].get(
                            "ticket_type"
                        )
                        if ticket_type:
                            # Get the normal ticket category from config
                            ticket_config = (
                                config.get("orgs", {})
                                .get("tickets", {})
                                .get(ticket_type, {})
                            )
                            normal_category_id = ticket_config.get("ticket_category")

                            if normal_category_id:
                                normal_category = bot.get_channel(normal_category_id)
                                if normal_category:
                                    # Update metadata - remove awaiting response flags
                                    ticket_metadata["ticket_config"][
                                        "awaiting_response"
                                    ] = False
                                    if (
                                        "awaiting_response_set_at"
                                        in ticket_metadata["ticket_config"]
                                    ):
                                        del ticket_metadata["ticket_config"][
                                            "awaiting_response_set_at"
                                        ]

                                    # Move channel back to normal category and update topic
                                    await ticket_channel.edit(
                                        category=normal_category,
                                        topic=json.dumps(ticket_metadata),
                                    )

                                    # Send notification in channel
                                    await ticket_channel.send(
                                        f"{message.author.mention} has responded. Ticket moved back to active category."
                                    )
                                    logger.info(
                                        f"Moved ticket {ticket_channel.name} back to active category after user response"
                                    )
                                else:
                                    logger.warning(
                                        f"Could not find normal category with ID {normal_category_id}"
                                    )
                            else:
                                logger.warning(
                                    f"No ticket_category found in config for ticket type {ticket_type}"
                                )
                except json.JSONDecodeError:
                    logger.warning(
                        f"Failed to parse ticket metadata from {ticket_channel.name}"
                    )
                except Exception as e:
                    logger.error(
                        f"Error handling awaiting response status for {ticket_channel.name}: {e}",
                        exc_info=True,
                    )

            await ticket_channel.send(embed=embed, files=files if files else None)
            logger.info(
                f"Forwarded DM from {message.author.global_name} to ticket {ticket_channel.name}"
            )
            # react to message
            await message.add_reaction("✅")

        except Exception as e:
            logger.error(f"Failed to forward DM to ticket: {e}")

    # Handle messages in ticket channels (forward to ticket owner if prefixed with "!reply")
    elif isinstance(message.channel, discord.TextChannel):
        # Check if message is in a ticket channel
        channel_name = message.channel.name

        # Ticket channels end with user ID (format: emoji-name-guild-userid)
        if "-" not in channel_name:
            logger.debug(f"Channel {channel_name} is not a ticket channel.")
            return

        try:
            ticket_owner_id = int(channel_name.split("-")[-1])
        except (ValueError, IndexError):
            logger.debug(f"Channel {channel_name} is not a valid ticket channel.")
            return

        # Check if message starts with !r or !rm prefix
        is_anonymous = message.content.startswith(
            "!r"
        ) and not message.content.startswith("!rm")
        message_content = (
            message.content[3:].strip()
            if not is_anonymous
            else message.content[2:].strip()
        )

        # Ignore messages that do not start with !r or !rm
        if not (message.content.startswith("!r") or message.content.startswith("!rm")):
            logger.debug("Message does not start with !r or !rm; ignoring.")
            return

        # Provide usage guidance when reply command is sent without content or files.
        if not message_content and not message.attachments:
            usage_message = "Usage: `!r <message>` to reply or with attachments."
            await message.channel.send(usage_message)
            return

        # Forward to ticket owner's DM
        try:
            ticket_owner = await bot.fetch_user(ticket_owner_id)
            ticket_channel = message.channel
            ticket_metadata = (
                json.loads(ticket_channel.topic) if ticket_channel.topic else None
            )

            ticket_color = ticket_metadata.get("color") if ticket_metadata else None

            embed = discord.Embed(
                description=truncate_text(
                    message_content, DISCORD_EMBED_DESCRIPTION_LIMIT
                ),
                color=(
                    discord.Color.green()
                    if not ticket_color
                    else discord.Color(int(ticket_color.lstrip("#"), 16))
                ),
            )

            if is_anonymous:
                embed.set_author(
                    name=truncate_text("Staff Member", DISCORD_EMBED_TITLE_LIMIT)
                )
                await DatabaseOperations.update_staff_response_count(
                    message.author.id, hidden=True
                )
            else:
                embed.set_author(
                    name=truncate_text(
                        message.author.global_name, DISCORD_EMBED_TITLE_LIMIT
                    ),
                    icon_url=message.author.display_avatar.url,
                )
                await DatabaseOperations.update_staff_response_count(
                    message.author.id, hidden=False
                )

            # Handle attachments
            files = []
            if message.attachments:
                for attachment in message.attachments:
                    embed.add_field(
                        name=truncate_text(
                            "Attachment", DISCORD_EMBED_FIELD_NAME_LIMIT
                        ),
                        value=truncate_text(
                            f"[{attachment.filename}]({attachment.url})",
                            DISCORD_EMBED_FIELD_VALUE_LIMIT,
                        ),
                        inline=False,
                    )
                    # Download and prepare file for forwarding
                    try:
                        file_data = await attachment.read()
                        files.append(
                            discord.File(
                                io.BytesIO(file_data), filename=attachment.filename
                            )
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to download attachment {attachment.filename}: {e}"
                        )

            # Collect embeds to send (message embed + any embeds from the original message)
            embeds_to_send = [embed]
            if message.embeds:
                embeds_to_send.extend(message.embeds)

            await ticket_owner.send(
                embeds=embeds_to_send, files=files if files else None
            )

            # delete message and replace with the received embed
            await message.delete()

            if is_anonymous:
                embed.set_author(
                    name=truncate_text(
                        f"{message.author.global_name} (hidden)",
                        DISCORD_EMBED_TITLE_LIMIT,
                    ),
                    icon_url=message.author.display_avatar.url,
                )
            await message.channel.send(embed=embed)

            logger.info(
                f"Forwarded ticket message from {message.author.name} to {ticket_owner.name}"
            )

        except discord.Forbidden:
            logger.warning(f"Cannot send DM to ticket owner (ID: {ticket_owner_id})")
            await message.add_reaction("❌")
        except Exception as e:
            logger.error(f"Failed to forward ticket message to DM: {e}")
            await message.add_reaction("❌")


@bot.event
async def on_command_error(ctx, error):
    """Suppress command not found errors for DM messages"""
    if isinstance(error, commands.CommandNotFound):
        # Ignore command not found errors (happens with empty prefix in DMs)
        return
    # Log other errors
    logger.error(f"Command error: {error}")


# Run the bot in a background thread
def start_discord_bot():
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN is not set in environment variables.")
        return
    bot.run(DISCORD_TOKEN)


async def init_tortoise():
    uri = os.getenv("PG_URI")
    await Tortoise.init(
        db_url=uri,
        modules={"models": ["utils.sql_utils"]},
    )
    await Tortoise.generate_schemas()
    logger.info("Tortoise ORM initialized and schemas generated.")


if __name__ == "__main__":
    logger.info("Starting Discord bot...")
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN is not set in environment variables.")
    else:
        bot.run(DISCORD_TOKEN)
