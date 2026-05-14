"""Subnet Service

Business logic for subnet management.
"""

import structlog  # type: ignore[import-untyped]

from ..core.entities import Subnet
from ..core.exceptions import SubnetNotFoundException
from ..core.interfaces import ISubnetRepository

logger = structlog.get_logger()


class SubnetService:
    """
    Subnet Service

    Orchestrates subnet-related business operations.
    Uses Repository pattern for persistence.
    """

    def __init__(self, subnet_repository: ISubnetRepository):
        """
        Initialize Subnet Service

        Args:
            subnet_repository: Subnet repository implementation
        """
        self.repository = subnet_repository

    async def create_subnet(
        self,
        subnet_id: str,
        name: str,
        owner: str,
        description: str | None = None,
        is_private: bool = False,
        security_config: dict | None = None,
        metadata: dict | None = None,
    ) -> Subnet:
        """
        Create a new subnet

        Args:
            subnet_id: Subnet identifier
            name: Subnet name
            owner: Subnet owner
            description: Subnet description
            is_private: Whether subnet is private
            security_config: Security configuration
            metadata: Additional metadata

        Returns:
            Created subnet entity

        Raises:
            ValueError: If subnet already exists
        """
        # Check if subnet already exists
        if await self.repository.exists(subnet_id):
            raise ValueError(f"Subnet {subnet_id} already exists")

        subnet = Subnet(
            subnet_id=subnet_id,
            name=name,
            owner=owner,
            description=description,
            is_private=is_private,
            security_config=security_config or {},
            metadata=metadata or {},
        )
        # The owner is implicitly a member: every ACL that keys off
        # `subnet.member_agent_ids` (private GET /tasks/{id}, accept_task,
        # is_subnet_member, ...) otherwise 403s the owner from acting on the
        # subnet they just created. Mirror this in the entity at construction
        # time so harness bootstrap (create_subnet → register_harness →
        # create_task) works without a follow-up join_subnet hack.
        subnet.add_member(owner)

        logger.info("create_subnet", subnet_id=subnet_id, name=name, owner=owner)
        await self.repository.save(subnet)
        return subnet

    async def get_subnet(self, subnet_id: str) -> Subnet:
        """
        Get subnet by ID

        Args:
            subnet_id: Subnet identifier

        Returns:
            Subnet entity

        Raises:
            SubnetNotFoundException: If subnet not found
        """
        subnet = await self.repository.find_by_id(subnet_id)
        if not subnet:
            raise SubnetNotFoundException(f"Subnet {subnet_id} not found")
        return subnet

    async def list_subnets(self, owner: str | None = None) -> list[Subnet]:
        """
        List subnets

        Args:
            owner: Optional owner filter

        Returns:
            List of subnets
        """
        if owner:
            return await self.repository.find_by_owner(owner)
        return await self.repository.find_all()

    async def list_public_subnets(self) -> list[Subnet]:
        """
        List all public subnets

        Returns:
            List of public subnets
        """
        return await self.repository.find_public_subnets()

    async def delete_subnet(self, subnet_id: str, owner: str) -> bool:
        """
        Delete a subnet

        Args:
            subnet_id: Subnet identifier
            owner: Owner identifier (for authorization check)

        Returns:
            True if deleted successfully

        Raises:
            SubnetNotFoundException: If subnet not found
            PermissionError: If owner doesn't match
        """
        subnet = await self.get_subnet(subnet_id)

        # Authorization check
        if subnet.owner != owner and owner != "system":
            raise PermissionError(f"Owner mismatch: {owner} != {subnet.owner}")

        # Prevent deletion of system subnets
        if subnet_id in ["public", "system"]:
            raise PermissionError(f"Cannot delete system subnet: {subnet_id}")

        logger.info("delete_subnet", subnet_id=subnet_id)
        return await self.repository.delete(subnet_id)

    async def add_member(self, subnet_id: str, agent_id: str) -> Subnet:
        """
        Add an agent to a subnet

        Args:
            subnet_id: Subnet identifier
            agent_id: Agent identifier

        Returns:
            Updated subnet entity
        """
        subnet = await self.get_subnet(subnet_id)
        subnet.add_member(agent_id)
        await self.repository.save(subnet)
        logger.info("subnet_member_added", subnet_id=subnet_id, agent_id=agent_id)
        return subnet

    async def remove_member(self, subnet_id: str, agent_id: str) -> Subnet:
        """
        Remove an agent from a subnet

        Args:
            subnet_id: Subnet identifier
            agent_id: Agent identifier

        Returns:
            Updated subnet entity
        """
        subnet = await self.get_subnet(subnet_id)
        subnet.remove_member(agent_id)
        await self.repository.save(subnet)
        logger.info("subnet_member_removed", subnet_id=subnet_id, agent_id=agent_id)
        return subnet

    async def get_member_count(self, subnet_id: str) -> int:
        """
        Get number of members in a subnet

        Args:
            subnet_id: Subnet identifier

        Returns:
            Number of members
        """
        subnet = await self.get_subnet(subnet_id)
        return subnet.get_member_count()

    async def exists(self, subnet_id: str) -> bool:
        """
        Check if subnet exists

        Args:
            subnet_id: Subnet identifier

        Returns:
            True if subnet exists
        """
        return await self.repository.exists(subnet_id)

    async def update_harness(
        self,
        subnet_id: str,
        owner: str,
        harness_url: str | None,
        harness_secret: str | None,
    ) -> Subnet:
        """
        Register or update the Org Harness webhook for a subnet.

        Only the subnet owner may register a harness. Pass ``harness_url=None``
        to unregister (subnet will fall back to platform-level webhook only).
        ``harness_secret`` is stored as-is and used to HMAC-sign outgoing
        webhook payloads delivered to ``harness_url``.

        Args:
            subnet_id: Subnet identifier
            owner: Authenticated agent making the request (for authz)
            harness_url: External webhook URL (or None to clear)
            harness_secret: HMAC secret (optional; None disables signing)

        Returns:
            Updated subnet entity

        Raises:
            SubnetNotFoundException: If subnet not found
            PermissionError: If owner mismatch
        """
        subnet = await self.get_subnet(subnet_id)

        if subnet.owner != owner and owner != "system":
            raise PermissionError(f"Owner mismatch: {owner} != {subnet.owner}")

        subnet.harness_url = harness_url
        subnet.harness_secret = harness_secret
        await self.repository.save(subnet)
        logger.info(
            "subnet_harness_updated",
            subnet_id=subnet_id,
            has_url=harness_url is not None,
            has_secret=harness_secret is not None,
        )
        return subnet
