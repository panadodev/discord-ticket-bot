import os
import aiohttp
from typing import Optional


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
                return data.get("steam_id")
            else:
                raise Exception(f"Unexpected status code: {response.status}")
