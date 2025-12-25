"""ACN API Schemas

Pydantic models for API request/response validation.

This module re-exports all models from the legacy models.py for backward compatibility.
Future: Split into agent.py, subnet.py, message.py, etc.
"""

# For now, re-export everything from models.py to maintain compatibility
# This allows gradual migration without breaking existing code
from ..models import (
    AgentCard,
    AgentInfo,
    AgentRegisterRequest,
    AgentRegisterResponse,
    AgentSearchRequest,
    AgentSearchResponse,
    SecurityScheme,
    Skill,
    SubnetCreateRequest,
    SubnetCreateResponse,
    SubnetInfo,
)

__all__ = [
    # Common
    "Skill",
    "AgentCard",
    "SecurityScheme",
    # Agent schemas
    "AgentInfo",
    "AgentRegisterRequest",
    "AgentRegisterResponse",
    "AgentSearchRequest",
    "AgentSearchResponse",
    # Subnet schemas
    "SubnetInfo",
    "SubnetCreateRequest",
    "SubnetCreateResponse",
]

# TODO: Future structure
# from .agent import (
#     AgentInfo,
#     AgentRegisterRequest,
#     AgentRegisterResponse,
#     AgentSearchRequest,
#     AgentSearchResponse,
# )
# from .subnet import (
#     SubnetInfo,
#     SubnetCreateRequest,
#     SubnetCreateResponse,
# )
# from .common import (
#     Skill,
#     AgentCard,
#     SecurityScheme,
# )
