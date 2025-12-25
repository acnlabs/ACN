"""Business Exceptions

Domain-specific exceptions.
"""


class ACNException(Exception):
    """Base exception for ACN"""

    pass


class AgentNotFoundException(ACNException):
    """Agent not found"""

    pass


class SubnetNotFoundException(ACNException):
    """Subnet not found"""

    pass
