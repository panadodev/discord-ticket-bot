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

    created = fields.IntField(null=False)


# SQL :
class DatabaseOperations:

    @staticmethod
    async def log_tickets(
        origin_org_guild: int,
        ticket_type: str,
        transcript: str,
        closed_by: int,
        opened_by: int,
        created_by: int,
    ):
        success = await tickets.create(
            origin_org_guild=origin_org_guild,
            ticket_type=ticket_type,
            transcript=transcript,
            closed_by=closed_by,
            opened_by=opened_by,
            created_by=created_by,
            created=int(time.time()),
        )
        if success:
            logger.info("Ticket logged successfully to the database.")
        else:
            logger.error("Failed to log ticket to the database.")

        return success

    @staticmethod
    async def tickets_handled_this_year(origin_org_guild: int):
        current_year = time.gmtime().tm_year
        start_of_year = int(
            time.mktime(time.strptime(f"{current_year}-01-01", "%Y-%m-%d"))
        )
        end_of_year = int(
            time.mktime(time.strptime(f"{current_year}-12-31", "%Y-%m-%d"))
        )

        count = await tickets.filter(
            origin_org_guild=origin_org_guild,
            created__gte=start_of_year,
            created__lte=end_of_year,
        ).count()

        return count
