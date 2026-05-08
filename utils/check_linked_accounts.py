import os
import aiohttp
from typing import Optional
import logging
from utils.sql_utils import DatabaseOperations

db = DatabaseOperations()
logger = logging.getLogger("main.py")


# Get linked accounts from any id from the api:
async def get_linked_accounts(user_id: int) -> Optional[dict]:
    """
    Query the link API to get linked accounts from a user ID (could be discord or steam)

    :param user_id: The user ID (Discord or Steam)
    :return: A dictionary with linked account information if found, None otherwise
    :raises: Exception if API token is invalid or other errors occur
    """
    token = os.getenv("API_TOKEN")
    if not token:
        raise ValueError("API_TOKEN environment variable not set")

    url = f"https://link.willjum.com/api/info/{user_id}"
    params = {"token": token}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as response:
            logger.debug(f"API response: {response}")
            if response.status == 401:
                raise Exception("Invalid API token")
            elif response.status == 404:
                return None  # No linked accounts found
            elif response.status == 200:
                data = await response.json()

                # Save to local database if we have both discord_id and steam_id
                discord_id = data.get("discord_id")
                steam_id = data.get("steam_id")
                if discord_id and steam_id:
                    await db.link_account(
                        discord_user_id=discord_id, steam_user_ids=[steam_id]
                    )

                return data  # Return the linked accounts data

            else:
                raise Exception(f"Unexpected status code: {response.status}")
