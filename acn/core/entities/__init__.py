"""Domain Entities

Pure business objects without framework dependencies.
These represent the core business concepts of ACN.
"""

from .agent import Agent, ClaimStatus
from .subnet import Subnet
from .task import (
    Participation,
    ParticipationStatus,
    Task,
    TaskStatus,
)

# Note: ``AgentStatus`` is intentionally no longer re-exported. It used
# to live on the ``Agent`` entity, but online-ness is now derived from
# the Redis alive key at read time (see ``AgentService.is_alive``). The
# API-layer enum with the same name still exists at ``acn.models``;
# import it from there if you need the response/SDK representation.

__all__ = [
    "Agent",
    "ClaimStatus",
    "Participation",
    "ParticipationStatus",
    "Subnet",
    "Task",
    "TaskStatus",
]
