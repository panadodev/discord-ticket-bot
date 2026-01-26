import logging
import os
import re
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord import Interaction, app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from utils.sql_utils import DatabaseOperations

load_dotenv()

logger = logging.getLogger("main.py")


class DiscordManager:
    