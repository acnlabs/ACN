"""Redis Persistence Layer

Concrete implementation of repositories using Redis.
"""

from .a2a_task_store import RedisTaskStore
from .agent_repository import RedisAgentRepository
from .allowlist_repository import RedisAllowlistRepository
from .follow_repository import RedisFollowRepository
from .subnet_repository import RedisSubnetRepository

# NB: ``RedisSubnetJoinRequestRepository`` and
# ``RedisSubnetAllowlistRepository`` are deliberately NOT re-exported
# here. Their module-level imports pull in the matching Postgres
# repositories (for the shared ``SubnetJoinRequestPendingError`` etc.),
# which in turn drag the rest of ``persistence/postgres/__init__.py``
# into the import graph. The ``acn/__init__.py`` top-level import of
# ``acn.infrastructure.messaging`` reaches this package via
# ``broadcast_service``, and the bigger Postgres chain creates a cycle
# back through ``services.billing_service`` → ``message_service`` →
# ``infrastructure.messaging``. Importing the two repos directly from
# their submodules at the call site (api.py composition root) avoids
# the cycle without restructuring the older billing/messaging imports.

__all__ = [
    "RedisAgentRepository",
    "RedisAllowlistRepository",
    "RedisFollowRepository",
    "RedisSubnetRepository",
    "RedisTaskStore",
]
