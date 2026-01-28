# @author: Panado (yesdotcom), 2026

import asyncio
import json
import logging
import os
import sys

import discord
import sentry_sdk
from discord.ext import commands
from dotenv import load_dotenv
from sentry_sdk.integrations.logging import EventHandler

from utils.discord_utils import CloseTicketButton, TicketSupportEmbedManager

# from tortoise import Tortoise


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


# Initialize Discord bot
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="", intents=intents)


@bot.event
async def on_ready():
    await bot.wait_until_ready()

    try:
        # Setup persistent views and ticket embed manager
        ticket_embed_manager = TicketSupportEmbedManager(bot)

        if not ticket_embed_manager.config:
            logger.error("Failed to load config")
            return

        archipel_guild_id = ticket_embed_manager.config.get("MAIN_GUILD_ID")
        if not archipel_guild_id:
            logger.error("MAIN_GUILD_ID is not set in config.json")
            return

        synced = await bot.tree.sync(guild=discord.Object(id=archipel_guild_id))
        logger.info(f"✅ Synced {len(synced)} command(s) to guild {archipel_guild_id}")

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


@bot.event
async def on_message(message: discord.Message):
    """Handle message forwarding between tickets and DMs"""
    # Ignore bot's own messages
    if message.author.bot:
        return

    # Process commands first
    await bot.process_commands(message)

    # Import needed utilities
    from utils.discord_utils import find_user_ticket, load_config, users_in_process

    config = load_config()
    if not config:
        return

    # Handle DM messages from users (forward to their ticket)
    if isinstance(message.channel, discord.DMChannel):
        # Don't forward if user is in ticket creation process
        if message.author.id in users_in_process:
            return

        # Find the user's guild (assuming main guild from config)
        main_guild_id = config.get("MAIN_GUILD_ID")
        if not main_guild_id:
            logger.error("MAIN_GUILD_ID not set in config")
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
                description=message.content,
                color=discord.Color.blue(),
            )
            embed.set_author(
                name=f"{message.author.global_name}",
                icon_url=message.author.display_avatar.url,
            )

            # Handle attachments
            if message.attachments:
                for attachment in message.attachments:
                    embed.add_field(
                        name="Attachment",
                        value=f"[{attachment.filename}]({attachment.url})",
                        inline=False,
                    )

            await ticket_channel.send(embed=embed)
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

        # Ticket channels end with user ID
        if "-" not in channel_name:
            logger.debug(f"Channel {channel_name} is not a ticket channel.")
            return

        parts = channel_name.split("-")
        try:
            ticket_owner_id = int(parts[-1])
        except (ValueError, IndexError):
            logger.debug(f"Channel {channel_name} is not a valid ticket channel.")
            return

        # Check if message starts with !r prefix
        if not message.content.startswith("!r"):
            logger.debug("Message does not start with !r prefix; not forwarding.")
            return

        # Remove the prefix from the message
        message_content = message.content[2:].strip()

        # Forward to ticket owner's DM
        try:
            ticket_owner = await bot.fetch_user(ticket_owner_id)
            ticket_channel = message.channel
            ticket_metadata = (
                json.loads(ticket_channel.topic) if ticket_channel.topic else None
            )

            ticket_color = ticket_metadata.get("color") if ticket_metadata else None

            embed = discord.Embed(
                description=message_content,
                color=(
                    discord.Color.green()
                    if not ticket_color
                    else discord.Color(int(ticket_color.lstrip("#"), 16))
                ),
            )

            embed.set_author(
                name=f"{message.author.global_name}",
                icon_url=message.author.display_avatar.url,
            )

            # Handle attachments
            if message.attachments:
                for attachment in message.attachments:
                    embed.add_field(
                        name="Attachment",
                        value=f"[{attachment.filename}]({attachment.url})",
                        inline=False,
                    )

            await ticket_owner.send(embed=embed)

            # delete message and replace with the received embed
            await message.delete()
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


"""async def init_tortoise():
    uri = os.getenv("PG_URI")
    await Tortoise.init(
        db_url=uri,
        modules={"models": ["utils.sql_utils"]},
    )
    await Tortoise.generate_schemas()
    logger.info("Tortoise ORM initialized and schemas generated.")"""


if __name__ == "__main__":
    logger.info("Starting Discord bot...")
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN is not set in environment variables.")
    else:
        bot.run(DISCORD_TOKEN)
