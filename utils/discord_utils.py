import asyncio
import io
import json
import logging

import discord
import DiscordTranscript
from discord import Interaction, app_commands
from discord.ext import commands
from dotenv import load_dotenv

from utils.sql_utils import DatabaseOperations

load_dotenv()

logger = logging.getLogger("main.py")


def load_config():
    with open("config.json", "r", encoding="utf-8") as f:
        config = json.load(f)["config"]
    if not config or config is None:
        logger.error("Failed to load configuration from config.json")
        return None
    return config


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
    ):
        super().__init__(
            label=button_name, custom_id=button_id, style=discord.ButtonStyle.primary
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
            logger.error(f"Error creating ticket for {user.name}: {e}")
            await interaction.followup.send(
                "❌ An error occurred while creating your ticket. Please try again later.",
                ephemeral=True,
            )
        finally:
            # Always remove user from in-process tracking
            users_in_process.discard(user.id)

    async def check_existing_ticket(
        self, guild: discord.Guild, user: discord.User, ticket_type: str
    ) -> discord.TextChannel | None:
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
    ) -> list[str] | None:
        """Send questions to user via DM and collect answers"""
        dm_channel = await user.create_dm()
        answers = []

        await dm_channel.send(
            "📝 **Ticket Creation Process**\n"
            "Please answer the following questions. You have 10 minutes to respond to each question. Please note the system is still a WIP, apologies for any inconvenience.\n"
            "Type `cancel` at any time to cancel the ticket creation."
        )

        for i, question in enumerate(questions, 1):
            embed = discord.Embed(
                title=f"Question {i}/{len(questions)}",
                description=question,
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

                answers.append(message.content)

            except TimeoutError:
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
        guild_clean = guild.name.lower().replace(" ", "-")
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
                "ticket_from_org": source_org_id,
                "ticket_from_guild": guild.id,
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
            logger.error(f"❌ Failed to create ticket channel: {e}")
            raise

        # Create summary embed
        embed = discord.Embed(
            title=f"{ticket_config['button_name']}",
            description=f"{user.mention}",
            color=discord.Color(
                int(ticket_config.get("embed_color", "#ffffff").lstrip("#"), 16)
            ),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text=f"{self.ticket_type}-{source_org} (User !r to reply)")

        # Add questions and answers
        questions = ticket_config.get("questions", [])
        for i, (question, answer) in enumerate(zip(questions, answers), 1):
            embed.add_field(name=f"{question}", value=answer, inline=False)

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
            logger.error(f"❌ Failed to send embed to ticket channel: {e}")

        # Notify user in DM
        try:
            dm_channel = await user.create_dm()
            await dm_channel.send(f"Server staff will assist you shortly.")
            logger.info(f"✅ Sent ticket confirmation DM to {user.name}")
        except discord.Forbidden:
            logger.warning(f"⚠️ Cannot send DM to {user.name} - DMs are disabled")
        except Exception as e:
            logger.error(f"❌ Failed to send DM notification: {e}")

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
        channel = interaction.channel

        # Check if the channel is a ticket channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ This can only be used in a ticket channel.", ephemeral=True
            )
            return

        # Verify the button was clicked by authorized user or staff
        # Extract the user_id from the channel name (format: emoji-tickettype-username-guild-userid)
        channel_name_parts = channel.name.split("-")
        try:
            ticket_owner_id = int(channel_name_parts[-1])
        except (ValueError, IndexError):
            await interaction.response.send_message(
                "❌ Could not determine ticket owner.", ephemeral=True
            )
            return

        # Check if the person clicking is the ticket owner or has manage channels permission
        if (
            interaction.user.id != ticket_owner_id
            and not interaction.user.guild_permissions.manage_channels
        ):
            await interaction.response.send_message(
                "❌ Only the ticket owner or staff can close this ticket.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Ticket is being closed by {interaction.user.mention}, generating transcript...",
            ephemeral=False,
        )

        logger.info(f"Ticket {channel.name} closed by {interaction.user.name}")

        # Delete the channel after a short delay
        channel_to_delete = channel  # Store reference for deletion after logging
        try:
            # log ticket using the channel's topic metadata
            channel_topic = channel.topic
            if channel_topic and channel_topic.startswith("{"):
                ticket_metadata = json.loads(channel_topic)
                # generate transcript
                result = await DiscordTranscript.export(channel, bot=self.bot)
                if not result:
                    logger.error("Failed to generate transcript.")
                else:
                    # send transcript as attachment to a log channel
                    log_channel = ticket_metadata["ticket_config"].get("log_channel")
                    if log_channel:
                        log_ch = interaction.guild.get_channel(log_channel)
                        if log_ch:
                            transcript_file = discord.File(
                                io.BytesIO(result.encode()),
                                filename=f"transcript-{channel.name}.html",
                            )
                            ticket_created_at = ticket_metadata["ticket_config"].get(
                                "created_at"
                            )
                            now = int(discord.utils.utcnow().timestamp())

                            ticket_duration = (
                                (now - ticket_created_at) / 3600
                                if ticket_created_at
                                else 0
                            )

                            # Get all pinned messages from the ticket channel
                            pinned_messages = await channel.pins()
                            pinned_content = ""
                            if pinned_messages:
                                pinned_content = "\n\n**Pinned Messages:**\n"
                                for pin_msg in pinned_messages:
                                    pinned_content += (
                                        f"- {pin_msg.author.name}: {pin_msg.content[:100]}...\n"
                                        if len(pin_msg.content) > 100
                                        else f"- {pin_msg.author.name}: {pin_msg.content}\n"
                                    )

                            await log_ch.send(
                                content=f"<t:{now}:R> {channel.name} \n Duration: {round(ticket_duration, 2)} hours.{pinned_content}",
                                file=transcript_file,
                            )

                            # Send pinned embeds to log channel
                            if pinned_messages:
                                for pin_msg in pinned_messages:
                                    if pin_msg.embeds:
                                        for embed in pin_msg.embeds:
                                            await log_ch.send(embed=embed)

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
                                    transcript=result,
                                    closed_by=interaction.user.id,
                                    opened_by=ticket_owner_id,
                                    created_by=ticket_metadata["ticket_config"][
                                        "created_by"
                                    ],
                                )
                            )
                            if success_saving_in_database is None:
                                logger.error("Failed to log ticket to database.")
                        except Exception as db_error:
                            logger.error(
                                f"⚠️ Failed to save ticket to database: {db_error}"
                            )
                    else:
                        logger.error(
                            f"Missing 'ticket_from_guild' in ticket metadata for channel: {channel.name}"
                        )
        except Exception as e:
            logger.error(f"❌ Error processing ticket closure: {e}")

        # Always delete the channel, regardless of logging success
        try:
            await asyncio.sleep(3)  # wait before deleting channel
            await channel_to_delete.delete()
            logger.info(f"✅ Deleted ticket channel: {channel_to_delete.name}")
        except Exception as e:
            logger.error(f"❌ Failed to delete ticket channel: {e}")

        # Notify ticket creator after deletion attempt
        try:
            ticket_owner = await self.bot.fetch_user(ticket_owner_id)
            await ticket_owner.send("Your ticket has been closed.")
        except Exception as e:
            logger.warning(f"⚠️ Could not notify ticket owner: {e}")


class TicketSupportEmbedManager:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = load_config()

    async def create_ticket_support_embed(self, channel: discord.TextChannel):
        embed = discord.Embed(
            title="Ticket Support",
            description="Press the buttons below for support.",
            color=discord.Color.blue(),
        )

        tickets_config = self.config["orgs"]["tickets"]  # type: ignore

        view = discord.ui.View(timeout=None)

        # Create buttons for each ticket type
        for ticket_type, ticket_info in tickets_config.items():
            button = TicketButton(
                button_name=ticket_info["button_name"],
                button_id=ticket_info["button_id"],
                ticket_type=ticket_type,
                config=self.config,
                bot=self.bot,
            )
            view.add_item(button)

        await channel.send(embed=embed, view=view)

    def create_persistent_view(self) -> discord.ui.View:
        """Create a persistent view with all ticket buttons for bot initialization"""
        if not self.config:
            return discord.ui.View(timeout=None)

        tickets_config = self.config["orgs"]["tickets"]
        view = discord.ui.View(timeout=None)

        for ticket_type, ticket_info in tickets_config.items():
            button = TicketButton(
                button_name=ticket_info["button_name"],
                button_id=ticket_info["button_id"],
                ticket_type=ticket_type,
                config=self.config,
                bot=self.bot,
            )
            view.add_item(button)

        return view


async def find_user_ticket(
    guild: discord.Guild, user_id: int
) -> discord.TextChannel | None:
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
        orgs_config = self.config.get("orgs", {})
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
            logger.error(f"Failed to update channel permissions: {e}")
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
        ticket_metadata = json.loads(channel.topic)
        ticket_metadata["ticket_config"]["button_name"] = ticket_config["button_name"]
        ticket_metadata["ticket_config"]["ticket_type"] = selected_type
        ticket_metadata["ticket_config"]["button_id"] = ticket_config["button_id"]
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

        try:
            # 5 second delay
            await asyncio.sleep(5)
            await channel.edit(topic=json.dumps(ticket_metadata))
        except Exception as e:
            logger.warning(f"Failed to update channel topic: {e}")

        # Send confirmation message
        embed = discord.Embed(
            title="✅ Ticket Reassigned",
            description=f"This ticket has been reassigned to **{selected_type}** team by {interaction.user.mention}",
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
    @app_commands.guilds(
        discord.Object(id=868656215834624020)
    )  # Hardcoded for registration
    @app_commands.describe()
    async def assign_ticket(self, interaction: Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        """Reassign a ticket to a different team"""

        # Check if config loaded successfully
        if not self.config:
            await interaction.followup.send(
                "❌ Main guild ID not configured.", ephemeral=True
            )
            logger.error("Config not loaded in assign_ticket")
            return

        # Ensure command is used in the main guild only
        if not interaction.guild or interaction.guild.id != self.config.get(
            "main_guild_id"
        ):
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
        if (
            not channel.topic
            or not channel.topic.startswith("{")
            or not channel.category.id == self.config.get("incoming_tickets_cat")
        ):
            await interaction.followup.send(
                "❌ This doesn't appear to be a valid ticket channel.", ephemeral=True
            )
            return

        # Get the current ticket type from the channel topic metadata
        ticket_metadata = json.loads(channel.topic)
        current_ticket_type = ticket_metadata["ticket_config"]["button_id"]

        # Create a select menu view excluding the current ticket type
        view = TicketTypeSelectView(
            {
                "orgs": {
                    "tickets": {
                        k: v
                        for k, v in self.config["orgs"]["tickets"].items()
                        if k != current_ticket_type
                    }
                }
            },
            self.bot,
        )

        await interaction.followup.send(
            "Select the ticket type to reassign this ticket to:",
            view=view,
            ephemeral=True,
        )
