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

    origin_org_guild = fields.IntField(max_length=20)

    ticket_type = fields.TextField(null=False)
    transcript = fields.TextField(null=True)

    closed_by = fields.IntField(max_length=20, null=False)
    opened_by = fields.IntField(max_length=20, null=False)
    created_by = fields.IntField(max_length=20, null=False)

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
