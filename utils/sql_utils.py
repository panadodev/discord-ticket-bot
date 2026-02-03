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
        orgs = await tickets.all().values_list("origin_org_guild", flat=True).distinct()
        
        for org in orgs:
            results[org] = {}
            ticket_types = await tickets.filter(origin_org_guild=org).values_list("ticket_type", flat=True).distinct()
            
            for t_type in ticket_types:
                response_times = await tickets.filter(
                    origin_org_guild=org,
                    ticket_type=t_type
                ).values_list("created", "made_at")
                
                total_response_time = sum(created - made_at for created, made_at in response_times)
                ticket_count = len(response_times)
                
                average_response_time = total_response_time / ticket_count if ticket_count > 0 else 0
                
                results[org][t_type] = {
                    "average_response_time": average_response_time,
                    "ticket_count": ticket_count
                }
        
        return results
        
