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
from tortoise import Tortoise

from utils.discord_utils import DiscordManager

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

        archipel_guild_id = os.getenv("ARCHIPEL_GUILD_ID")
        if not archipel_guild_id:
            logger.error("ARCHIPEL_GUILD_ID is not set in environment variables.")
            return
        archipel_guild_id = int(archipel_guild_id)
        synced = await bot.tree.sync(guild=discord.Object(id=archipel_guild_id))
        logger.info(f"✅ Synced {len(synced)} command(s) to guild {archipel_guild_id}")

        # Debug: Print command names
        for cmd in synced:
            logger.info(f"🔧 Command synced: {cmd.name}")
    except Exception as e:
        logger.error(f"❌ Failed to sync commands or add cog: {e}")


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


async def startup_event():
    logger.info("Discord app...")
    await init_tortoise()

    # Initialize Discord bot in a background thread
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN is not set in environment variables.")
        return
    else:
        DISCORD_TOKEN = str(DISCORD_TOKEN)

    loop = asyncio.get_event_loop()
    loop.create_task(bot.start(DISCORD_TOKEN))

    logger.info("Startup event completed.")
