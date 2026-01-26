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
    type = fields.TextField()

    guid = fields.IntField(max_length=20)

    ticket_type = fields.TextField(null=False)
    transcript = fields.TextField(null=True)

    closed_by = fields.IntField(max_length=20, null=False)
    opened_by = fields.IntField(max_length=20, null=False)
    created_by = fields.IntField(max_length=20, null=False)

    created = fields.IntField(null=False)


# SQL :
class DatabaseOperations:

    @staticmethod
    async def log_pvp(data: dict):
        await tickets.create(
            type="pvp",
            guid=data["guid"],
            ticket_type=data["ticket_type"],
            transcript=data.get("transcript", ""),
            closed_by=data["closed_by"],
            opened_by=data["opened_by"],
            created_by=data["created_by"],
            created=int(time.time()),
        )
