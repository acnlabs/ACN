"""A2A protocol entry-point package.

Re-exports the application factory and the PR #2 from_agent
validation middleware so wiring code in ``api.py`` can import from
a single, stable path.
"""

from .auth_middleware import A2AFromAgentValidationMiddleware
from .server import ACNAgentExecutor, create_a2a_app

__all__ = [
    "A2AFromAgentValidationMiddleware",
    "ACNAgentExecutor",
    "create_a2a_app",
]
