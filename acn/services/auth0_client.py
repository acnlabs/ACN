"""
Auth0 Agent Credential Client

调用 Backend 的 Agent Auth API，为 Agent 创建/查询/吊销 Auth0 M2M 凭证。
"""

import httpx
import structlog
from pydantic import BaseModel

logger = structlog.get_logger()


class Auth0CredentialResult(BaseModel):
    """Auth0 凭证操作结果"""

    success: bool
    client_id: str | None = None
    client_secret: str | None = None
    token_endpoint: str | None = None
    audience: str | None = None
    already_exists: bool = False
    dev_mode: bool = False
    error: str | None = None


class Auth0CredentialClient:
    """Auth0 Agent Credential Client"""

    def __init__(
        self,
        backend_url: str,
        timeout: float = 15.0,
        internal_token: str | None = None,
    ):
        self.backend_url = backend_url.rstrip("/")
        self.timeout = timeout
        self.internal_token = internal_token

    def _get_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.internal_token:
            headers["X-Internal-Token"] = self.internal_token
        return headers

    async def create_credentials(
        self,
        agent_id: str,
        agent_name: str,
    ) -> Auth0CredentialResult:
        """
        为 Agent 创建 Auth0 M2M 凭证

        Args:
            agent_id: Agent ID
            agent_name: Agent 名称

        Returns:
            Auth0CredentialResult 包含 client_id/secret
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.post(
                    f"{self.backend_url}/api/agent-auth/credentials",
                    headers=self._get_headers(),
                    json={
                        "agent_id": agent_id,
                        "agent_name": agent_name,
                    },
                )

                if response.status_code == 200:
                    try:
                        data = response.json()
                    except Exception:
                        data = {}
                    logger.info(
                        "agent_auth0_credentials_created",
                        agent_id=agent_id,
                        client_id=data.get("client_id"),
                        already_exists=data.get("already_exists", False),
                    )
                    return Auth0CredentialResult(
                        success=True,
                        client_id=data.get("client_id"),
                        client_secret=data.get("client_secret"),
                        token_endpoint=data.get("token_endpoint"),
                        audience=data.get("audience"),
                        already_exists=data.get("already_exists", False),
                        dev_mode=data.get("dev_mode", False),
                    )
                else:
                    error = self._extract_error(response)
                    logger.warning(
                        "agent_auth0_credentials_failed",
                        agent_id=agent_id,
                        status_code=response.status_code,
                        error=error,
                    )
                    return Auth0CredentialResult(
                        success=False,
                        error=str(error),
                    )

        except httpx.RequestError as e:
            logger.error(
                "agent_auth0_credentials_error",
                agent_id=agent_id,
                error=str(e),
            )
            return Auth0CredentialResult(
                success=False,
                error=str(e),
            )

    async def revoke_credentials(self, agent_id: str) -> bool:
        """吊销 Agent 的 Auth0 凭证"""
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.delete(
                    f"{self.backend_url}/api/agent-auth/credentials/{agent_id}",
                    headers=self._get_headers(),
                )
                return response.status_code == 200
        except httpx.RequestError:
            return False

    @staticmethod
    def _extract_error(response: httpx.Response) -> str:
        try:
            return response.json().get("detail", response.text)
        except Exception:
            return response.text or f"HTTP {response.status_code}"
