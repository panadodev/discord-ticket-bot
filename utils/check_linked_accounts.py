import os
import aiohttp
from typing import Optional

from utils.sql_utils import DatabaseOperations

db = DatabaseOperations()


# Pull from main database, will crate local record if not exists
async def get_steam_id_from_discord(discord_id: int) -> Optional[int]:
    """
    Query the link API to get the steam_id from a discord_id

    :param discord_id: The Discord user ID
    :return: The Steam ID if found, None otherwise
    :raises: Exception if API token is invalid or other errors occur
    """
    token = os.getenv("API_TOKEN")
    if not token:
        raise ValueError("API_TOKEN environment variable not set")

    url = f"https://link.willjum.com/api/info/{discord_id}"
    params = {"token": token}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as response:
            if response.status == 401:
                raise Exception("Invalid API token")
            elif response.status == 404:
                return None  # No link found
            elif response.status == 200:
                data = await response.json()
                steam_id = data.get("steam_id")
                if steam_id:
                    # log to local:
                    await db.link_account(
                        discord_user_id=discord_id, steam_user_ids=[steam_id]
                    )
                return steam_id

            else:
                raise Exception(f"Unexpected status code: {response.status}")


# get linked accounts from any id from the api:
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

    url = f"https://link.willjum.com/api/linked_accounts/{user_id}"
    params = {"token": token}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as response:
            if response.status == 401:
                raise Exception("Invalid API token")
            elif response.status == 404:
                return None  # No linked accounts found
            elif response.status == 200:
                data = await response.json()
                return data  # Return the linked accounts data

            else:
                raise Exception(f"Unexpected status code: {response.status}")
