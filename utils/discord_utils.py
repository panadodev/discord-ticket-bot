import asyncio
import io
import json
import logging

import discord
import DiscordTranscript
from discord.ext import commands
from dotenv import load_dotenv

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
        guild = interaction.guild

        if not guild:
            await interaction.response.send_message(
                "❌ This command can only be used in a server.", ephemeral=True
            )
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
                f"❌ You already have an open ticket: {existing_ticket.mention}",
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

            # Create the ticket channel
            await self.create_ticket_channel(guild, user, ticket_config, answers)

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
    ) -> None:
        """Create the ticket channel with proper permissions and summary"""
        # Get categories
        incoming_cat_id = self.config.get("incoming_tickets_cat")
        incoming_category = (
            guild.get_channel(incoming_cat_id) if incoming_cat_id else None
        )

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
        channel_name = f"{self.ticket_type}-{username_clean}-{guild_clean}-{user.id}"
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
            user: discord.PermissionOverwrite(
                read_messages=True, send_messages=True, read_message_history=True
            ),
        }

        # Add permissions for viewable roles
        orgs_config = self.config.get("orgs", {})
        for role_key in ticket_config.get("viewable_by", []):
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
                    overwrites[role] = discord.PermissionOverwrite(
                        read_messages=True,
                        send_messages=True,
                        read_message_history=True,
                    )

        button_name = ticket_config.get("button_name", "Unknown Ticket")
        button_id = ticket_config.get("button_id", "unknown_ticket")
        origin_org = guild_clean
        created_at = int(discord.utils.utcnow().timestamp())
        ticket_metadata = {
            "ticket_config": {
                "button_name": button_name,
                "button_id": button_id,
                "log_channel": ticket_config.get("log_channel", None),
                "ticket_channel_icon": ticket_icon,
                "ticket_from_org": origin_org,
                "viewable_by": ticket_config.get("viewable_by", []),
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
        embed.footer.text = f"{guild.name}-{user.id}-{user.name}"

        # Add questions and answers
        questions = ticket_config.get("questions", [])
        for i, (question, answer) in enumerate(zip(questions, answers), 1):
            embed.add_field(name=f"{question}:", value=answer, inline=False)

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
            logger.info(f"✅ Sent ticket summary embed to {ticket_channel.name}")
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
        # notify ticket creator, continue if fails
        try:
            ticket_owner = await self.bot.fetch_user(ticket_owner_id)
            await ticket_owner.send("Your ticket has been closed.")
        except Exception as e:
            logger.warning(f"⚠️ Could not notify ticket owner: {e}")

        logger.info(f"Ticket {channel.name} closed by {interaction.user.name}")

        # Delete the channel after a short delay
        try:

            # log ticket using the channel's topic metadata
            channel_topic = channel.topic
            if channel_topic and channel_topic.startswith("{"):
                ticket_metadata = json.loads(channel_topic)
                # generate transcript
                result = await DiscordTranscript.export(channel, bot=self.bot)
                if not result:
                    logger.error("Failed to generate transcript.")
                    return
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
                            (now - ticket_created_at) / 3600 if ticket_created_at else 0
                        )
                        await log_ch.send(
                            content=f"Transcript for closed ticket {channel.name} \n Duration: {round(ticket_duration, 2)} hours.",
                            file=transcript_file,
                        )
                        await asyncio.sleep(5)
                        await channel.delete()
                        logger.info(f"✅ Deleted ticket channel: {channel.name}")

        except Exception as e:
            logger.error(f"❌ Failed to delete ticket channel {channel.name}: {e}")


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


class DiscordCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = load_config()
