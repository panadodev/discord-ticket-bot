# @author: Panado (yesdotcom), 2026

import asyncio
import logging
import os
import sys

import discord
import sentry_sdk
from discord.ext import commands
from dotenv import load_dotenv
from sentry_sdk.integrations.logging import EventHandler

from utils.discord_utils import DiscordManager, TicketSupportEmbedManager

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

# Instantiate DiscordManager (not the bot itself, just API utilities)
discord_manager = DiscordManager()

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
