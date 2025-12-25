"""Subnet Repository Interface

Defines contract for subnet persistence operations.
"""

from abc import ABC, abstractmethod

from ..entities import Subnet


class ISubnetRepository(ABC):
    """
    Abstract interface for Subnet persistence
    
    Infrastructure layer provides concrete implementation.
    """

    @abstractmethod
    async def save(self, subnet: Subnet) -> None:
        """
        Save or update a subnet
        
        Args:
            subnet: Subnet entity to save
        """
        pass

    @abstractmethod
    async def find_by_id(self, subnet_id: str) -> Subnet | None:
        """
        Find subnet by ID
        
        Args:
            subnet_id: Subnet identifier
            
        Returns:
            Subnet entity or None if not found
        """
        pass

    @abstractmethod
    async def find_all(self) -> list[Subnet]:
        """
        Find all subnets
        
        Returns:
            List of all subnet entities
        """
        pass

    @abstractmethod
    async def find_by_owner(self, owner: str) -> list[Subnet]:
        """
        Find all subnets owned by a user/system
        
        Args:
            owner: Subnet owner identifier
            
        Returns:
            List of subnets owned by the user
        """
        pass

    @abstractmethod
    async def find_public_subnets(self) -> list[Subnet]:
        """
        Find all public subnets
        
        Returns:
            List of public subnets
        """
        pass

    @abstractmethod
    async def delete(self, subnet_id: str) -> bool:
        """
        Delete a subnet
        
        Args:
            subnet_id: Subnet identifier
            
        Returns:
            True if deleted, False if not found
        """
        pass

    @abstractmethod
    async def exists(self, subnet_id: str) -> bool:
        """
        Check if subnet exists
        
        Args:
            subnet_id: Subnet identifier
            
        Returns:
            True if subnet exists
        """
        pass

