import asyncio
import io
import json
import logging
import os
from typing import Optional

import discord
import DiscordTranscript
from discord import Interaction, app_commands
from discord.ext import commands
from dotenv import load_dotenv

from utils.sql_utils import DatabaseOperations

load_dotenv()

logger = logging.getLogger("main.py")

# Discord text limits
DISCORD_EMBED_TITLE_LIMIT = 256
DISCORD_EMBED_DESCRIPTION_LIMIT = 4096
DISCORD_EMBED_FIELD_NAME_LIMIT = 256
DISCORD_EMBED_FIELD_VALUE_LIMIT = 1024
DISCORD_MESSAGE_LIMIT = 2000


def truncate_text(text: str, max_length: int, suffix: str = "...") -> str:
    """Truncate text to fit Discord's limits"""
    if not text:
        return ""
    text = str(text)
    if len(text) <= max_length:
        return text
    return text[:max_length - len(suffix)] + suffix


def load_config() -> Optional[dict]:
    """Load configuration from config.json"""
    try:
        if not os.path.exists("config.json"):
            logger.error("config.json file not found")
            return None

        with open("config.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            config = data.get("config")

        if not config:
            logger.error(
                "Failed to load configuration from config.json - 'config' key missing"
            )
            return None

        return config
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse config.json: {e}")
        return None
    except Exception as e:
        logger.error(f"Error loading config.json: {e}")
        return None


# Track users currently in ticket creation process
users_in_process = set()


class TicketButton(discord.ui.Button):
    def __init__(
        self,
        button_name: str,
        button_id: str,
        ticket_type: str,
        config: dict,
        bot: commands.Bot,
        button_style: str = "PRIMARY",
    ):
        # Map button style strings to discord.ButtonStyle enums
        style_map = {
            "PRIMARY": discord.ButtonStyle.primary,
            "SECONDARY": discord.ButtonStyle.secondary,
            "SUCCESS": discord.ButtonStyle.success,
            "DANGER": discord.ButtonStyle.danger,
        }
        
        # Get the style from the map, default to primary if not found
        discord_style = style_map.get(button_style.upper(), discord.ButtonStyle.primary)
        
        super().__init__(
            label=button_name, custom_id=button_id, style=discord_style
        )
        self.ticket_type = ticket_type
        self.config = config
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        """Handle ticket button click"""
        user = interaction.user

        # Always use the main guild for ticket creation
        main_guild_id = self.config.get("main_guild_id")
        if not main_guild_id:
            await interaction.response.send_message(
                "❌ Main guild ID not configured.", ephemeral=True
            )
            logger.error("Main guild ID not configured in config.json")
            return

        guild = self.bot.get_guild(main_guild_id)
        if not guild:
            await interaction.response.send_message(
                "❌ Could not access the main guild.", ephemeral=True
            )
            logger.error(f"Bot cannot access guild with ID {main_guild_id}")
            return

        # Check if user is already in a ticket creation process
        if user.id in users_in_process:
            await interaction.response.send_message(
                "❌ You are already in the middle of creating a ticket. Please complete or cancel that process first.",
                ephemeral=True,
            )
            return

        # Get ticket configuration
        ticket_config = self.config["orgs"]["tickets"][self.ticket_type]
        ticket_icon = ticket_config["ticket_channel_icon"]

        # Check if user already has an open ticket
        existing_ticket = await self.check_existing_ticket(
            guild, user, self.ticket_type
        )
        if existing_ticket:
            await interaction.response.send_message(
                f"❌ You already have an open ticket.",
                ephemeral=True,
            )
            logger.info(
                f"User {user.name} tried to create duplicate {self.ticket_type} ticket"
            )
            return

        # Mark user as in process
        users_in_process.add(user.id)

        # Acknowledge the interaction
        await interaction.response.send_message(
            f"✅ Starting {self.ticket_type} ticket process. Check your DMs!",
            ephemeral=True,
        )

        # Try to DM the user with questions
        try:
            answers = await self.collect_answers_via_dm(
                user, ticket_config["questions"]
            )

            if answers is None:
                # User cancelled or timeout
                return

            source_org = interaction.guild.name.lower().replace(" ", "-")
            source_org_id = interaction.guild.id

            # Create the ticket channel
            await self.create_ticket_channel(
                guild, user, ticket_config, answers, source_org, source_org_id
            )

        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I cannot send you DMs. Please enable DMs from server members and try again.",
                ephemeral=True,
            )
            logger.warning(f"Cannot DM user {user.name} - DMs are disabled")
        except Exception as e:
            logger.error(f"Error creating ticket for {user.name}: {e}", exc_info=True)
            await interaction.followup.send(
                "❌ An error occurred while creating your ticket. Please try again later.",
                ephemeral=True,
            )
        finally:
            # Always remove user from in-process tracking
            users_in_process.discard(user.id)

    async def check_existing_ticket(
        self, guild: discord.Guild, user: discord.User, ticket_type: str
    ) -> Optional[discord.TextChannel]:
        """Check if user already has an open ticket of the same type"""
        # Search for channels matching the pattern: emoji-tickettype-username-...-userid
        username_clean = user.name.lower().replace(" ", "-")
        # Remove special characters from username for matching
        username_clean = "".join(c for c in username_clean if c.isalnum() or c in "-_")

        for channel in guild.text_channels:
            # Channel name format is: emoji-tickettype-username-guild-userid
            # Check if the channel contains the ticket type and ends with user ID
            channel_lower = channel.name.lower()

            # Remove emoji from start (emojis are usually 1-2 characters, skip them)
            # Then check if it starts with ticket_type and ends with user ID
            if f"-{ticket_type}-" in channel_lower and channel_lower.endswith(
                str(user.id)
            ):
                # Additional check: ensure username is in the channel name
                if username_clean in channel_lower:
                    return channel

        return None

    async def collect_answers_via_dm(
        self, user: discord.User, questions: list[str]
    ) -> Optional[list[str]]:
        """Send questions to user via DM and collect answers"""
        dm_channel = await user.create_dm()
        answers = []

        welcome_dm = self.config.get("welcome_dm")
        await dm_channel.send(
            truncate_text(
                welcome_dm,
                DISCORD_MESSAGE_LIMIT
            )
        )

        for i, question in enumerate(questions, 1):
            embed = discord.Embed(
                title=truncate_text(f"Question {i}/{len(questions)}", DISCORD_EMBED_TITLE_LIMIT),
                description=truncate_text(question, DISCORD_EMBED_DESCRIPTION_LIMIT),
                color=discord.Color.blue(),
            )
            await dm_channel.send(embed=embed)

            # Wait for user response
            def check(m):
                return m.author == user and m.channel == dm_channel

            try:
                message = await self.bot.wait_for(
                    "message", check=check, timeout=600.0  # 10 minutes
                )

                if message.content.lower() == "cancel":
                    await dm_channel.send("❌ Ticket creation cancelled.")
                    logger.info(f"User {user.name} cancelled ticket creation")
                    return None

                # Truncate answer to fit Discord limits (will be used in embed field)
                truncated_answer = truncate_text(message.content, DISCORD_EMBED_FIELD_VALUE_LIMIT)
                answers.append(truncated_answer)
                
                # Notify user if their answer was truncated
                if len(message.content) > DISCORD_EMBED_FIELD_VALUE_LIMIT:
                    await dm_channel.send(
                        f"⚠️ Your answer was too long and has been truncated to {DISCORD_EMBED_FIELD_VALUE_LIMIT} characters."
                    )

            except asyncio.TimeoutError:
                await dm_channel.send("⏱️ Ticket creation timed out. Please try again.")
                logger.info(f"Ticket creation timed out for {user.name}")
                return None

        await dm_channel.send("✅ All questions answered! Creating your ticket now...")
        return answers

    async def create_ticket_channel(
        self,
        guild: discord.Guild,
        user: discord.User,
        ticket_config: dict,
        answers: list[str],
        source_org: str,
        source_org_id: int,
    ) -> None:
        """Create the ticket channel with proper permissions and summary"""
        # Get categories
        incoming_cat_id = self.config.get("incoming_tickets_cat")
        if not incoming_cat_id:
            logger.error("Incoming tickets category ID not configured.")
            raise ValueError("Incoming tickets category ID not configured.")
        incoming_category = guild.get_channel(incoming_cat_id)

        # Create channel name: tickettype-username-org-dcid
        ticket_icon = ticket_config["ticket_channel_icon"]

        logger.info(
            f"Ticket icon from config: '{ticket_icon}' (type: {type(ticket_icon)})"
        )

        # Convert Discord emoji syntax to actual emoji, or use emoji directly
        emoji_map = {
            ":green_circle:": "🟢",
            ":orange_circle:": "🟠",
            ":blue_circle:": "🔵",
            ":red_circle:": "🔴",
            ":yellow_circle:": "🟡",
            ":purple_circle:": "🟣",
        }

        # Check if it's Discord syntax (starts with :) or already an emoji
        if ticket_icon.startswith(":"):
            emoji = emoji_map.get(ticket_icon, "🎫")
        else:
            # It's already an emoji, use it directly
            emoji = ticket_icon

        logger.info(f"Mapped emoji: '{emoji}'")

        # Create sanitized channel name using ticket type
        username_clean = user.name.lower().replace(" ", "-")
        guild_clean = source_org.lower().replace(" ", "-")
        channel_name = f"{username_clean}-{guild_clean}-{user.id}"
        # Remove any special characters that Discord doesn't allow
        channel_name = "".join(c for c in channel_name if c.isalnum() or c in "-_")

        # Add emoji prefix to channel name
        full_channel_name = f"{emoji}-{channel_name}"

        logger.info(f"Creating ticket channel: {full_channel_name}")

        # Setup permissions
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(
                read_messages=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                embed_links=True,
            ),
        }

        # Add permissions for viewable roles
        orgs_config = self.config.get("orgs", {})
        designated_role = ticket_config.get("designated")
        for role_key in ticket_config.get("has_perms", []):
            # Search for the role in all orgs
            role_id = None
            for org_name, org_data in orgs_config.items():
                if org_name == "tickets":
                    continue
                if "roles" in org_data and role_key in org_data["roles"]:
                    role_id = org_data["roles"][role_key]
                    break

            if role_id:
                role = guild.get_role(role_id)
                if role:
                    # Designated role gets full permissions, others get read-only
                    if role_key == designated_role:
                        overwrites[role] = discord.PermissionOverwrite(
                            read_messages=True,
                            send_messages=True,
                            read_message_history=True,
                        )
                    else:
                        overwrites[role] = discord.PermissionOverwrite(
                            read_messages=True,
                            send_messages=False,
                            read_message_history=True,
                        )

        button_name = ticket_config.get("button_name", "Unknown Ticket")
        button_id = ticket_config.get("button_id", "unknown_ticket")
        created_at = int(discord.utils.utcnow().timestamp())
        ticket_metadata = {
            "ticket_config": {
                "button_name": button_name,
                "button_id": button_id,
                "log_channel": ticket_config.get("log_channel", None),
                "ticket_type": self.ticket_type,
                "ticket_channel_icon": ticket_icon,
                "ticket_from_guild": source_org_id,
                "has_perms": ticket_config.get("has_perms", []),
                "created_at": created_at,
                "created_by": user.id,
                "color": ticket_config.get("embed_color", "#ffffff"),
            }
        }

        # Create the ticket channel with emoji icon
        try:
            ticket_channel = await guild.create_text_channel(
                name=full_channel_name,
                category=incoming_category,
                overwrites=overwrites,
                topic=json.dumps(ticket_metadata),
            )
            logger.info(
                f"✅ Created ticket channel: {ticket_channel.name} (ID: {ticket_channel.id})"
            )
            # Wait a moment for permissions to propagate
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"❌ Failed to create ticket channel: {e}", exc_info=True)
            raise

        # Create summary embed
        embed = discord.Embed(
            title=truncate_text(guild_clean + ticket_config['button_name'], DISCORD_EMBED_TITLE_LIMIT),
            description=truncate_text(f"{user.mention}", DISCORD_EMBED_DESCRIPTION_LIMIT),
            color=discord.Color(
                int(ticket_config.get("embed_color", "#ffffff").lstrip("#"), 16)
            ),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text=truncate_text(f"{self.ticket_type}-{source_org} (Use !r to reply)", DISCORD_EMBED_TITLE_LIMIT))

        # Add questions and answers
        questions = ticket_config.get("questions", [])
        for i, (question, answer) in enumerate(zip(questions, answers), 1):
            embed.add_field(
                name=truncate_text(question, DISCORD_EMBED_FIELD_NAME_LIMIT), 
                value=truncate_text(answer, DISCORD_EMBED_FIELD_VALUE_LIMIT), 
                inline=False
            )

        # Create close button view
        close_button = CloseTicketButton(self.bot)
        view = discord.ui.View(timeout=None)
        view.add_item(close_button)

        # Send the summary
        try:
            message = await ticket_channel.send(
                embed=embed,
                view=view,
            )
            # Pin the embed message
            await message.pin()
            logger.info(
                f"✅ Sent and pinned ticket summary embed to {ticket_channel.name}"
            )
        except Exception as e:
            logger.error(
                f"❌ Failed to send embed to ticket channel: {e}", exc_info=True
            )

        # Notify user in DM
        try:
            dm_channel = await user.create_dm()
            await dm_channel.send(f"Server staff will assist you shortly.")
            logger.info(f"✅ Sent ticket confirmation DM to {user.name}")
        except discord.Forbidden:
            logger.warning(f"⚠️ Cannot send DM to {user.name} - DMs are disabled")
        except Exception as e:
            logger.error(f"❌ Failed to send DM notification: {e}", exc_info=True)

        logger.info(f"Created ticket channel {channel_name} for user {user.name}")


class CloseTicketButton(discord.ui.Button):
    def __init__(self, bot: commands.Bot):
        super().__init__(
            label="Close Ticket",
            custom_id="close_ticket_button",
            style=discord.ButtonStyle.danger,
        )
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        """Handle close ticket button click"""
        # Defer immediately to prevent interaction timeout
        await interaction.response.defer()

        channel = interaction.channel

        # Check if the channel is a ticket channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                "❌ This can only be used in a ticket channel.", ephemeral=True
            )
            return

        # Verify the button was clicked by authorized user or staff
        # Extract the user_id from the channel name (format: emoji-tickettype-username-guild-userid)
        channel_name_parts = channel.name.split("-")
        try:
            ticket_owner_id = int(channel_name_parts[-1])
        except (ValueError, IndexError):
            await interaction.followup.send(
                "❌ Could not determine ticket owner.", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"Ticket is being closed by {interaction.user.mention}, generating transcript...",
            ephemeral=False,
        )

        logger.info(f"Ticket {channel.name} closed by {interaction.user.name}")

        # Store channel reference and generate transcript BEFORE deletion
        channel_name = channel.name
        guild_id = interaction.guild.id
        transcript_result = None
        ticket_metadata = None

        try:
            # Parse ticket metadata from channel topic
            channel_topic = channel.topic
            if channel_topic and channel_topic.startswith("{"):
                ticket_metadata = json.loads(channel_topic)

                # Generate transcript before any deletion
                transcript_result = await DiscordTranscript.export(
                    channel, bot=self.bot
                )

                if not transcript_result:
                    logger.error("Failed to generate transcript.")
                else:
                    logger.info(f"✅ Generated transcript for {channel_name}")

        except Exception as e:
            logger.error(f"❌ Error generating transcript: {e}", exc_info=True)

        # Now delete the channel
        try:
            await asyncio.sleep(3)
            await channel.delete()
            logger.info(f"✅ Deleted ticket channel: {channel_name}")
        except Exception as e:
            logger.error(f"❌ Failed to delete ticket channel: {e}", exc_info=True)
            # Continue with logging even if deletion fails

        # Log ticket after channel deletion
        if transcript_result and ticket_metadata:
            try:
                log_channel_id = ticket_metadata["ticket_config"].get("log_channel")
                if log_channel_id:
                    log_ch = interaction.guild.get_channel(log_channel_id)
                    if log_ch:
                        transcript_file = discord.File(
                            io.BytesIO(transcript_result.encode()),
                            filename=f"transcript-{channel_name}.html",
                        )
                        ticket_created_at = ticket_metadata["ticket_config"].get(
                            "created_at"
                        )
                        now = int(discord.utils.utcnow().timestamp())

                        ticket_duration = (
                            (now - ticket_created_at) / 3600 if ticket_created_at else 0
                        )

                        # Get pinned messages info (channel is deleted, use stored data if available)
                        pinned_content = ""

                        log_message = truncate_text(
                            f"<t:{now}:R> {channel_name} \n Duration: {round(ticket_duration, 2)} hours.",
                            DISCORD_MESSAGE_LIMIT
                        )
                        await log_ch.send(
                            content=log_message,
                            file=transcript_file,
                        )
                        logger.info(f"✅ Logged transcript to log channel")

                logger.info("Logging ticket to database")
                origin_org_guild = ticket_metadata["ticket_config"].get(
                    "ticket_from_guild"
                )
                if origin_org_guild:
                    ticket_type = ticket_metadata["ticket_config"]["ticket_type"]
                    try:
                        success_saving_in_database = (
                            await DatabaseOperations.log_tickets(
                                origin_org_guild=origin_org_guild,
                                ticket_type=ticket_type,
                                transcript=transcript_result,
                                closed_by=interaction.user.id,
                                opened_by=ticket_owner_id,
                                made_at=ticket_metadata["ticket_config"]["created_at"],
                                created_by=ticket_metadata["ticket_config"]["created_by"],
                            )
                        )
                        if success_saving_in_database is None:
                            logger.error("Failed to log ticket to database.")
                        else:
                            logger.info(f"✅ Logged ticket to database")
                    except Exception as db_error:
                        logger.error(
                            f"⚠️ Failed to save ticket to database: {db_error}",
                            exc_info=True,
                        )
                else:
                    logger.error(
                        f"Missing 'ticket_from_guild' in ticket metadata for channel: {channel_name}"
                    )
            except Exception as e:
                logger.error(
                    f"❌ Error processing ticket closure logging: {e}", exc_info=True
                )

        # Notify ticket creator after deletion
        try:
            ticket_owner = await self.bot.fetch_user(ticket_owner_id)
            await ticket_owner.send("Your ticket has been closed.")
        except Exception as e:
            logger.warning(f"⚠️ Could not notify ticket owner: {e}")

        # Update bot status with current ticket count
        try:
            count = await DatabaseOperations.tickets_handled_this_year(
                origin_org_guild=guild_id
            )
            await self.bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name=f"{count} tickets handled this year",
                )
            )
        except Exception as e:
            logger.error(f"❌ Failed to update bot status: {e}", exc_info=True)


class TicketSupportEmbedManager:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = load_config()

    async def create_ticket_support_embed(self, channel: discord.TextChannel):
        if not self.config:
            logger.error("Cannot create ticket support embed - config not loaded")
            return

        embed = discord.Embed(
            title=truncate_text("Ticket Support", DISCORD_EMBED_TITLE_LIMIT),
            description=truncate_text("Press the buttons below for support.", DISCORD_EMBED_DESCRIPTION_LIMIT),
            color=discord.Color.blue(),
        )

        tickets_config = self.config["orgs"]["tickets"]

        view = discord.ui.View(timeout=None)

        # Create buttons for each ticket type
        for ticket_type, ticket_info in tickets_config.items():
            button = TicketButton(
                button_name=ticket_info["button_name"],
                button_id=ticket_info["button_id"],
                ticket_type=ticket_type,
                config=self.config,
                bot=self.bot,
                button_style=ticket_info.get("button_style", "PRIMARY"),
            )
            view.add_item(button)

        await channel.send(embed=embed, view=view)

    def create_persistent_view(self) -> discord.ui.View:
        """Create a persistent view with all ticket buttons for bot initialization"""
        view = discord.ui.View(timeout=None)

        if not self.config:
            logger.error("Cannot create persistent view - config not loaded")
            return view

        tickets_config = self.config["orgs"]["tickets"]

        for ticket_type, ticket_info in tickets_config.items():
            button = TicketButton(
                button_name=ticket_info["button_name"],
                button_id=ticket_info["button_id"],
                ticket_type=ticket_type,
                config=self.config,
                bot=self.bot,
                button_style=ticket_info.get("button_style", "PRIMARY"),
            )
            view.add_item(button)

        return view


async def find_user_ticket(
    guild: discord.Guild, user_id: int
) -> Optional[discord.TextChannel]:
    """Find the most recently created ticket channel for a user"""
    user_tickets = []

    for channel in guild.text_channels:
        # Channel name format: emoji-tickettype-username-guild-userid
        if channel.name.endswith(str(user_id)):
            user_tickets.append(channel)

    if not user_tickets:
        return None

    # Return the most recently created ticket (highest ID = most recent)
    return max(user_tickets, key=lambda c: c.id)


class TicketTypeSelect(discord.ui.Select):
    def __init__(self, config: dict, bot: commands.Bot):
        self.config = config
        self.bot = bot

        # Build options from config
        tickets_config = config["orgs"]["tickets"]
        options = []
        for ticket_type, ticket_info in tickets_config.items():
            options.append(
                discord.SelectOption(
                    label=ticket_info["button_name"],
                    value=ticket_type,
                    description=f"Assign to {ticket_info['designated']} team",
                    emoji=ticket_info.get("ticket_channel_icon", "🎫"),
                )
            )

        super().__init__(
            placeholder="Select ticket type to assign to...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        """Handle ticket type selection"""
        await interaction.response.defer()
        channel = interaction.channel

        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                "❌ This can only be used in a ticket channel.", ephemeral=True
            )
            return

        # Get the selected ticket type
        selected_type = self.values[0]
        ticket_config = self.config["orgs"]["tickets"][selected_type]

        # Get the current ticket metadata to find old roles
        old_viewable_roles = []
        if channel.topic and channel.topic.startswith("{"):
            try:
                ticket_metadata = json.loads(channel.topic)
                old_viewable_roles = ticket_metadata["ticket_config"].get(
                    "has_perms", []
                )
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse channel topic for {channel.name}")

        # Always use the main guild for role lookups
        main_guild_id = self.config.get("main_guild_id")
        if not main_guild_id:
            await interaction.followup.send(
                "❌ Main guild ID not configured.", ephemeral=True
            )
            logger.error("Main guild ID not configured in config.json")
            return

        guild = self.bot.get_guild(main_guild_id)
        if not guild:
            await interaction.followup.send(
                "❌ Could not access the main guild.", ephemeral=True
            )
            logger.error(f"Bot cannot access guild with ID {main_guild_id}")
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(
                read_messages=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                embed_links=True,
            ),
        }

        # Explicitly deny access to roles from the old ticket type that are no longer viewable
        orgs_config = self.config.get("orgs", {})
        for old_role_key in old_viewable_roles:
            # Only deny if it's not in the new viewable roles
            if old_role_key not in ticket_config.get("has_perms", []):
                role_id = None
                for org_name, org_data in orgs_config.items():
                    if org_name == "tickets":
                        continue
                    if "roles" in org_data and old_role_key in org_data["roles"]:
                        role_id = org_data["roles"][old_role_key]
                        break

                if role_id:
                    role = guild.get_role(role_id)
                    if role:
                        # Explicitly deny this role's access
                        overwrites[role] = discord.PermissionOverwrite(
                            read_messages=False
                        )

        # Add permissions for viewable roles
        designated_role = ticket_config.get("designated")
        for role_key in ticket_config.get("has_perms", []):
            role_id = None
            for org_name, org_data in orgs_config.items():
                if org_name == "tickets":
                    continue
                if "roles" in org_data and role_key in org_data["roles"]:
                    role_id = org_data["roles"][role_key]
                    break

            if role_id:
                role = guild.get_role(role_id)
                if role:
                    # Designated role gets full permissions, others get read-only
                    if role_key == designated_role:
                        overwrites[role] = discord.PermissionOverwrite(
                            read_messages=True,
                            send_messages=True,
                            read_message_history=True,
                        )
                    else:
                        overwrites[role] = discord.PermissionOverwrite(
                            read_messages=True,
                            send_messages=False,
                            read_message_history=True,
                        )

        # Update channel permissions
        try:
            # Apply all overwrites at once to replace old permissions
            await channel.edit(overwrites=overwrites)
        except Exception as e:
            logger.error(f"Failed to update channel permissions: {e}", exc_info=True)
            await interaction.followup.send(
                "❌ Failed to update channel permissions.", ephemeral=True
            )
            return

        # Update channel name with new emoji
        ticket_icon = ticket_config["ticket_channel_icon"]
        emoji_map = {
            ":green_circle:": "🟢",
            ":orange_circle:": "🟠",
            ":blue_circle:": "🔵",
            ":red_circle:": "🔴",
            ":yellow_circle:": "🟡",
            ":purple_circle:": "🟣",
        }

        if ticket_icon.startswith(":"):
            emoji = emoji_map.get(ticket_icon, "🎫")
        else:
            emoji = ticket_icon

        # Replace the emoji at the start of the channel name
        old_name = channel.name
        # Remove old emoji (first part before first dash)
        name_parts = old_name.split("-", 1)
        if len(name_parts) > 1:
            new_name = f"{emoji}-{name_parts[1]}"
            try:
                await channel.edit(name=new_name)
            except Exception as e:
                logger.warning(f"Failed to update channel name: {e}")

        # Update channel topic metadata
        if channel.topic and channel.topic.startswith("{"):
            try:
                ticket_metadata = json.loads(channel.topic)
                ticket_metadata["ticket_config"]["button_name"] = ticket_config[
                    "button_name"
                ]
                ticket_metadata["ticket_config"]["ticket_type"] = selected_type
                ticket_metadata["ticket_config"]["button_id"] = ticket_config[
                    "button_id"
                ]
                ticket_metadata["ticket_config"]["log_channel"] = ticket_config.get(
                    "log_channel"
                )
                ticket_metadata["ticket_config"]["ticket_channel_icon"] = ticket_icon
                ticket_metadata["ticket_config"]["has_perms"] = ticket_config.get(
                    "has_perms", []
                )
                ticket_metadata["ticket_config"]["color"] = ticket_config.get(
                    "embed_color", "#ffffff"
                )

                # 5 second delay
                await asyncio.sleep(5)
                await channel.edit(topic=json.dumps(ticket_metadata))
            except Exception as e:
                logger.warning(f"Failed to update channel topic: {e}", exc_info=True)

        # Send confirmation message
        embed = discord.Embed(
            title=truncate_text("✅ Ticket Reassigned", DISCORD_EMBED_TITLE_LIMIT),
            description=truncate_text(f"This ticket has been reassigned to **{selected_type}** team by {interaction.user.mention}", DISCORD_EMBED_DESCRIPTION_LIMIT),
            color=discord.Color(
                int(ticket_config.get("embed_color", "#ffffff").lstrip("#"), 16)
            ),
        )

        await interaction.followup.send(embed=embed)
        logger.info(
            f"Ticket {channel.name} reassigned to {selected_type} by {interaction.user.name}"
        )


class TicketTypeSelectView(discord.ui.View):
    def __init__(self, config: dict, bot: commands.Bot):
        super().__init__(timeout=60)
        self.add_item(TicketTypeSelect(config, bot))


class DiscordCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = load_config()

        # Get main_guild_id for command registration
        if self.config:
            self.main_guild_id = self.config.get("main_guild_id")
        else:
            self.main_guild_id = None
            logger.error("Failed to load config in DiscordCommands")

    @app_commands.command(name="assign")
    @app_commands.describe()
    async def assign_ticket(self, interaction: Interaction) -> None:
        """Reassign a ticket to a different team"""
        await interaction.response.defer(ephemeral=True)

        # Check if config loaded successfully
        if not self.config:
            await interaction.followup.send(
                "❌ Configuration not loaded. Please contact an administrator.",
                ephemeral=True,
            )
            logger.error("Config not loaded in assign_ticket")
            return

        # Ensure command is used in the main guild only
        main_guild_id = self.config.get("main_guild_id")
        if not interaction.guild or interaction.guild.id != main_guild_id:
            await interaction.followup.send(
                "❌ This command can only be used in the main server.", ephemeral=True
            )
            return

        channel = interaction.channel

        # Check if this is a ticket channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                "❌ This command can only be used in a ticket channel.", ephemeral=True
            )
            return

        # Verify it's a ticket channel by checking the topic
        incoming_cat_id = self.config.get("incoming_tickets_cat")
        if (
            not channel.topic
            or not channel.topic.startswith("{")
            or not channel.category
            or channel.category.id != incoming_cat_id
        ):
            await interaction.followup.send(
                "❌ This doesn't appear to be a valid ticket channel.", ephemeral=True
            )
            return

        # Get the current ticket type from the channel topic metadata
        try:
            ticket_metadata = json.loads(channel.topic)
            current_ticket_type = ticket_metadata["ticket_config"]["button_id"]
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Failed to parse ticket metadata: {e}")
            await interaction.followup.send(
                "❌ Could not read ticket information.", ephemeral=True
            )
            return

        # Create a filtered config with only other ticket types
        filtered_tickets = {
            k: v
            for k, v in self.config["orgs"]["tickets"].items()
            if k != current_ticket_type
        }

        if not filtered_tickets:
            await interaction.followup.send(
                "❌ No other ticket types available to reassign to.", ephemeral=True
            )
            return

        # Create filtered config with complete structure
        filtered_config = {
            "orgs": {
                "tickets": filtered_tickets,
                **{
                    k: v
                    for k, v in self.config.get("orgs", {}).items()
                    if k != "tickets"
                },
            },
            "main_guild_id": self.config.get("main_guild_id"),
            "incoming_tickets_cat": self.config.get("incoming_tickets_cat"),
        }

        # Create a select menu view excluding the current ticket type
        view = TicketTypeSelectView(filtered_config, self.bot)

        await interaction.followup.send(
            "Select the ticket type to reassign this ticket to:",
            view=view,
            ephemeral=True,
        )
        
    @app_commands.command(name="close_ticket")
    async def close_ticket_command(self, interaction: Interaction) -> None:
        """Close the current ticket"""
        close_button = CloseTicketButton(self.bot)
        await close_button.callback(interaction)
        
        
    @app_commands.command(name="average_ticket_duration")
    async def average_ticket_duration(self, interaction: Interaction) -> None:
        """Get the average ticket duration for the current year"""
        await interaction.response.defer(ephemeral=True)

        if not self.config:
            await interaction.followup.send(
                "❌ Configuration not loaded. Please contact an administrator.",
                ephemeral=True,
            )
            logger.error("Config not loaded in average_ticket_duration")
            return
        
        # check if management
        if not interaction.guild or interaction.guild.id != self.config.get("main_guild_id"):
            await interaction.followup.send(
                "❌ This command can only be used in the main server.", ephemeral=True
            )
            return
        # check user is management
        management_role_id = self.config.get("management_role_id")
        if management_role_id:
            management_role = interaction.guild.get_role(management_role_id)
            if management_role not in interaction.user.roles:
                await interaction.followup.send(
                    "❌ You do not have permission to use this command.", ephemeral=True
                )
                return

        main_guild_id = self.config.get("main_guild_id")
        if not main_guild_id:
            await interaction.followup.send(
                "❌ Main guild ID not configured.", ephemeral=True
            )
            logger.error("Main guild ID not configured in config.json")
            return

        try:
            avg_duration = await DatabaseOperations.average_respond_times()
            if avg_duration is None or not avg_duration:
                await interaction.followup.send(
                    "❌ Failed to calculate average ticket duration or no tickets found.", ephemeral=True
                )
                logger.error("Failed to calculate average ticket duration")
                return

            # Create an embed for each organization
            for org_id, ticket_types in avg_duration.items():
                # Try to get the guild name, fallback to ID if not found
                guild = self.bot.get_guild(org_id)
                org_name = guild.name if guild else f"Guild ID: {org_id}"
                
                embed = discord.Embed(
                    title=truncate_text(f"📊 Average Ticket Duration - {org_name}", DISCORD_EMBED_TITLE_LIMIT),
                    description=truncate_text("Average response times by ticket type", DISCORD_EMBED_DESCRIPTION_LIMIT),
                    color=discord.Color.blue(),
                )
                
                # Add a field for each ticket type
                for ticket_type, stats in ticket_types.items():
                    avg_seconds = stats["average_response_time"]
                    ticket_count = stats["ticket_count"]
                    
                    # Convert seconds to a human-readable format
                    hours = int(avg_seconds // 3600)
                    minutes = int((avg_seconds % 3600) // 60)
                    seconds = int(avg_seconds % 60)
                    
                    if hours > 0:
                        formatted_time = f"{hours}h {minutes}m {seconds}s"
                    elif minutes > 0:
                        formatted_time = f"{minutes}m {seconds}s"
                    else:
                        formatted_time = f"{seconds}s"
                    
                    field_value = f"⏱️ **{formatted_time}**\n📋 Tickets: {ticket_count}"
                    
                    embed.add_field(
                        name=truncate_text(ticket_type.replace("_", " ").title(), DISCORD_EMBED_FIELD_NAME_LIMIT),
                        value=truncate_text(field_value, DISCORD_EMBED_FIELD_VALUE_LIMIT),
                        inline=True
                    )
                
                # Send the embed for this organization
                await interaction.followup.send(embed=embed, ephemeral=True)
                
        except Exception as e:
            logger.error(f"Error calculating average ticket duration: {e}", exc_info=True)
            await interaction.followup.send(
                "❌ An error occurred while calculating average ticket duration.",
                ephemeral=True,
            )
