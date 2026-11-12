"""Resolve AgentPlanet private task board membership."""

from urllib.parse import quote

import httpx


async def check_private_board_access(
    client: httpx.AsyncClient,
    backend_url: str,
    internal_token: str,
    board_id: str,
    principal_id: str,
) -> bool:
    """Allow access only when AgentPlanet explicitly confirms membership."""
    if not internal_token or not principal_id:
        return False
    path = f"/admin/labs/boards/{quote(board_id, safe='')}/access"
    try:
        response = await client.post(
            f"{backend_url.rstrip('/')}{path}",
            headers={"X-Internal-Token": internal_token},
            json={"principal_id": principal_id},
            timeout=3.0,
        )
        response.raise_for_status()
        result = response.json()
    except (httpx.HTTPError, ValueError):
        return False
    return isinstance(result, dict) and result.get("allowed") is True
