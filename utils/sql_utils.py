import logging
import time

from dotenv import load_dotenv
from tortoise import fields
from tortoise.models import Model

load_dotenv()

# Setup logger
logger = logging.getLogger("main.py")


# Tortoise SQL models:
class tickets(Model):
    class Meta:
        table = "tickets"

    id = fields.IntField(pk=True)

    origin_org_guild = fields.BigIntField()

    ticket_type = fields.TextField(null=False)
    transcript = fields.TextField(null=True)

    closed_by = fields.BigIntField(null=False)
    opened_by = fields.BigIntField(null=False)
    created_by = fields.BigIntField(null=False)

    made_at = fields.IntField(null=False)
    created = fields.IntField(null=False)


class staff_response_count(Model):
    class Meta:
        table = "staff_response_count"

    user_id = fields.BigIntField(pk=True)
    response_count = fields.IntField(default=0)
    hidden_response_count = fields.IntField(default=0)
    last_response_time = fields.IntField(default=0)


class blacklist(Model):
    class Meta:
        table = "blacklist"

    user_id = fields.BigIntField(pk=True)
    reason = fields.TextField(null=True)
    blacklisted_by = fields.BigIntField(null=False)
    blacklisted_at = fields.IntField(null=False)


class linked_accounts(Model):
    class Meta:
        table = "linked_accounts"

    discord_user_id = fields.BigIntField(pk=True)
    steam_user_ids = fields.JSONField(null=False)

    created_at = fields.IntField(null=False)


# SQL :
class DatabaseOperations:

    @staticmethod
    async def log_tickets(
        origin_org_guild: int,
        ticket_type: str,
        transcript: str,
        closed_by: int,
        opened_by: int,
        made_at: int,
        created_by: int,
    ):
        success = await tickets.create(
            origin_org_guild=origin_org_guild,
            ticket_type=ticket_type,
            transcript=transcript,
            closed_by=closed_by,
            opened_by=opened_by,
            created_by=created_by,
            made_at=made_at,
            created=int(time.time()),
        )
        if success:
            logger.info("Ticket logged successfully to the database.")
        else:
            logger.error("Failed to log ticket to the database.")

        return success

    @staticmethod
    async def tickets_handled_this_year():
        current_year = time.gmtime().tm_year
        start_of_year = int(
            time.mktime(time.strptime(f"{current_year}-01-01", "%Y-%m-%d"))
        )
        end_of_year = int(
            time.mktime(time.strptime(f"{current_year}-12-31", "%Y-%m-%d"))
        )

        count = await tickets.filter(
            created__gte=start_of_year,
            created__lte=end_of_year,
        ).count()

        return count

    @staticmethod
    async def average_respond_times():
        """
        {"org_a": {
        "ticket_type_a": {
            "average_response_time": 123,
            "ticket_count": 10
            },
        "ticket_type_b": {
            "average_response_time": 456,
            "ticket_count": 5
            }
            },
        "org_b": {

        },
        """
        results = {}
        orgs = await tickets.all().distinct().values_list("origin_org_guild", flat=True)

        for org in orgs:
            results[org] = {}
            ticket_types = (
                await tickets.filter(origin_org_guild=org)
                .distinct()
                .values_list("ticket_type", flat=True)
            )

            for t_type in ticket_types:
                response_times = await tickets.filter(
                    origin_org_guild=org, ticket_type=t_type
                ).values_list("created", "made_at")

                total_response_time = sum(
                    created - made_at for created, made_at in response_times
                )
                ticket_count = len(response_times)

                average_response_time = (
                    total_response_time / ticket_count if ticket_count > 0 else 0
                )

                results[org][t_type] = {
                    "average_response_time": average_response_time,
                    "ticket_count": ticket_count,
                }

        return results

    @staticmethod
    async def update_staff_response_count(user_id: int, hidden: bool):
        staff = await staff_response_count.get_or_none(user_id=user_id)
        if not staff:
            staff = await staff_response_count.create(user_id=user_id)

        if hidden:
            staff.hidden_response_count += 1
        else:
            staff.response_count += 1

        staff.last_response_time = int(time.time())
        await staff.save()

    @staticmethod
    async def count_staff_responses(user_id: int):
        staff_record = await staff_response_count.get_or_none(user_id=user_id)
        total_tickets = await tickets.filter(opened_by=user_id).count()

        now = int(time.time())
        thirty_days_ago = now - (30 * 24 * 60 * 60)

        tickets_last_30_days = await tickets.filter(
            opened_by=user_id,
            created__gte=thirty_days_ago,
        ).count()

        last_ticket = (
            await tickets.filter(opened_by=user_id).order_by("-created").first()
        )

        # Return tickets handled, tickets in last 30 days, last ticket,
        # hidden message responses, and visible message responses.
        return {
            "tickets_handled": total_tickets,
            "tickets_last_30_days": tickets_last_30_days,
            "last_ticket": last_ticket.id if last_ticket else None,
            "hidden_message_responses": (
                staff_record.hidden_response_count if staff_record else 0
            ),
            "visible_message_responses": (
                staff_record.response_count if staff_record else 0
            ),
        }

    @staticmethod
    async def get_all_staff_performance():
        """Get performance stats for all staff members"""
        all_staff = await staff_response_count.all()
        staff_stats = []

        for staff_member in all_staff:
            user_id = staff_member.user_id

            # Count tickets closed by this staff member
            total_tickets_closed = await tickets.filter(closed_by=user_id).count()

            # Get last closed ticket
            last_closed_ticket = (
                await tickets.filter(closed_by=user_id).order_by("-created").first()
            )

            staff_stats.append(
                {
                    "user_id": user_id,
                    "tickets_closed": total_tickets_closed,
                    "last_ticket_time": (
                        last_closed_ticket.created if last_closed_ticket else None
                    ),
                    "last_message_time": staff_member.last_response_time,
                    "hidden_message_responses": staff_member.hidden_response_count,
                    "visible_message_responses": staff_member.response_count,
                }
            )

        # Sort by tickets closed (descending)
        staff_stats.sort(key=lambda x: x["tickets_closed"], reverse=True)

        return staff_stats

    @staticmethod
    async def get_blacklist_status(user_id: int):
        record = await blacklist.get_or_none(user_id=user_id)
        if record:
            return {
                "is_blacklisted": True,
                "reason": record.reason,
                "blacklisted_by": record.blacklisted_by,
                "blacklisted_at": record.blacklisted_at,
            }
        else:
            return {"is_blacklisted": False}

    @staticmethod
    async def add_to_blacklist(user_id: int, reason: str, blacklisted_by: int):
        existing_record = await blacklist.get_or_none(user_id=user_id)
        if existing_record:
            existing_record.reason = reason
            existing_record.blacklisted_by = blacklisted_by
            existing_record.blacklisted_at = int(time.time())
            await existing_record.save()
            return existing_record
        else:
            new_record = await blacklist.create(
                user_id=user_id,
                reason=reason,
                blacklisted_by=blacklisted_by,
                blacklisted_at=int(time.time()),
            )
            return new_record

    @staticmethod
    async def remove_from_blacklist(user_id: int):
        record = await blacklist.get_or_none(user_id=user_id)
        if record:
            await record.delete()
            return True
        return False

    @staticmethod
    async def check_linked_account(discord_user_id: int):
        record = await linked_accounts.get_or_none(discord_user_id=discord_user_id)
        if record:
            return {
                "linked": True,
                "steam_user_ids": record.steam_user_ids,
                "created_at": record.created_at,
            }
        else:
            return {"linked": False}

    @staticmethod
    async def link_account(discord_user_id: int, steam_user_ids: list):
        existing_record = await linked_accounts.get_or_none(
            discord_user_id=discord_user_id
        )
        if existing_record:
            existing_record.steam_user_ids = steam_user_ids
            existing_record.created_at = int(time.time())
            await existing_record.save()
            return existing_record
        else:
            new_record = await linked_accounts.create(
                discord_user_id=discord_user_id,
                steam_user_ids=steam_user_ids,
                created_at=int(time.time()),
            )
            return new_record
