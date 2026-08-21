"""GitHub device flow authentication - headless OAuth for CLI and apps."""

import os
import logging
import httpx
from typing import Optional, Dict
from datetime import datetime, timedelta

log = logging.getLogger("github_device_flow")

# GitHub OAuth credentials from environment
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")

# GitHub API endpoints
DEVICE_CODE_ENDPOINT = "https://github.com/login/device/code"
TOKEN_ENDPOINT = "https://github.com/login/oauth/access_token"
USER_ENDPOINT = "https://api.github.com/user"

# Device flow configuration
DEVICE_FLOW_TIMEOUT = 900  # 15 minutes
DEVICE_FLOW_POLL_INTERVAL = 5  # seconds


class DeviceFlowError(Exception):
    """GitHub device flow error."""
    pass


class PendingDeviceFlow:
    """In-memory storage for pending device flows (production should use Redis)."""
    _flows: Dict[str, dict] = {}

    @classmethod
    def create(cls, device_code: str, user_code: str, verification_uri: str) -> None:
        """Store a pending device flow."""
        cls._flows[device_code] = {
            "user_code": user_code,
            "verification_uri": verification_uri,
            "created_at": datetime.utcnow(),
            "access_token": None,
            "github_user": None,
        }
        log.info(f"Device flow created: {user_code}")

    @classmethod
    def get(cls, device_code: str) -> Optional[dict]:
        """Retrieve a pending device flow."""
        flow = cls._flows.get(device_code)
        if flow and (datetime.utcnow() - flow["created_at"]).total_seconds() > DEVICE_FLOW_TIMEOUT:
            del cls._flows[device_code]
            return None
        return flow

    @classmethod
    def set_token(cls, device_code: str, access_token: str, github_user: dict) -> None:
        """Mark flow as authorized with access token."""
        flow = cls.get(device_code)
        if flow:
            flow["access_token"] = access_token
            flow["github_user"] = github_user
            log.info(f"Device flow authorized: {github_user['login']}")

    @classmethod
    def cleanup(cls, device_code: str) -> None:
        """Remove a device flow."""
        cls._flows.pop(device_code, None)


async def request_device_code() -> Dict:
    """
    Step 1: Request a device code and user code from GitHub.

    Returns:
    {
        "device_code": "...",
        "user_code": "...",
        "verification_uri": "https://github.com/login/device",
        "expires_in": 900,
        "interval": 5
    }
    """
    if not GITHUB_CLIENT_ID:
        raise DeviceFlowError("GITHUB_CLIENT_ID not configured")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                DEVICE_CODE_ENDPOINT,
                data={"client_id": GITHUB_CLIENT_ID, "scope": "user:email"},
                headers={"Accept": "application/json"},
                timeout=30.0,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            log.error(f"GitHub device code request failed: {e}")
            raise DeviceFlowError(f"Failed to request device code: {e}")


async def poll_device_flow(device_code: str) -> Optional[Dict]:
    """
    Step 2: Poll GitHub to see if user has authorized the device code.

    Returns access token response if authorized, None if still pending,
    raises DeviceFlowError if denied or other error.
    """
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        raise DeviceFlowError("GitHub credentials not configured")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                TOKEN_ENDPOINT,
                data={
                    "client_id": GITHUB_CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_flow",
                },
                headers={"Accept": "application/json"},
                timeout=30.0,
            )

            if response.status_code == 200:
                data = response.json()
                return data  # Contains "access_token"

            elif response.status_code == 401:
                # Still waiting for user authorization or device code expired
                data = response.json()
                error = data.get("error", "")
                if error == "authorization_pending":
                    return None  # Still waiting
                elif error == "expired_token":
                    raise DeviceFlowError("Device code expired. Request a new one.")
                elif error == "access_denied":
                    raise DeviceFlowError("User denied authorization.")
                else:
                    raise DeviceFlowError(f"Authorization error: {error}")

            else:
                response.raise_for_status()
                return None

        except httpx.HTTPError as e:
            log.error(f"Device flow poll failed: {e}")
            raise DeviceFlowError(f"Failed to poll GitHub: {e}")


async def get_github_user(access_token: str) -> Dict:
    """
    Step 3: Get authenticated user's GitHub profile.

    Returns:
    {
        "id": 123456,
        "login": "username",
        "name": "User Name",
        "email": "user@example.com",
        ...
    }
    """
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                USER_ENDPOINT,
                headers={
                    "Authorization": f"token {access_token}",
                    "Accept": "application/vnd.github.v3+json",
                },
                timeout=30.0,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            log.error(f"GitHub user fetch failed: {e}")
            raise DeviceFlowError(f"Failed to get user info: {e}")
