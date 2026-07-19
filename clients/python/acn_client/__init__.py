"""
ACN Client - Official Python SDK for Agent Collaboration Network

Example:
    >>> from acn_client import ACNClient
    >>>
    >>> async with ACNClient("http://localhost:9000") as client:
    ...     agents = await client.search_agents(skills=["coding"])
    ...     print(f"Found {len(agents)} agents")
"""

from .client import ACNClient, ACNError
from .models import (
    KNOWN_INBOX_MESSAGE_STATUSES,
    KNOWN_PAYMENT_TASK_STATUSES,
    AgentInfo,
    AgentJoinRequest,
    AgentJoinResponse,
    AgentRegisterRequest,
    AgentSearchOptions,
    AgentStatus,
    AttentionFee,
    BroadcastRequest,
    BroadcastStrategy,
    CommunicationProfile,
    ManifestContentResponse,
    ManifestEntry,
    ManifestSendRequest,
    MessageType,
    ParticipationInfo,
    PaymentCapability,
    PaymentMethod,
    PaymentNetwork,
    PaymentTask,
    SendMessageRequest,
    SessionEntry,
    SessionInviteRequest,
    SubnetInfo,
    TaskAcceptRequest,
    TaskAcceptResponse,
    TaskCreateRequest,
    TaskInfo,
    TaskReviewRequest,
    TaskSubmitRequest,
)
from .realtime import ACNRealtime, ACNRealtimeOptions, AuthMode, WSState
from .regions import (
    ACN_HOSTED_URLS,
    hosted_base_url,
    normalize_base_url,
    resolve_hosted_base_url,
)

# Single source of truth: pyproject.toml ``[project].version``.
# Hard-coded ``__version__`` strings drifted by 3 minor versions in the past
# (pyproject said 0.10.0 while this file still said 0.7.1), making bug
# reports and telemetry lie about the running SDK build. Reading via
# importlib.metadata at import time eliminates that class of drift entirely.
# The fallback only fires for non-installed checkouts (e.g. running tests
# straight from source without ``pip install -e .``) — published wheels
# always populate the metadata.
try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("acn-client")
except PackageNotFoundError:  # pragma: no cover — only hits in uninstalled source trees
    __version__ = "0.0.0+unknown"

__all__ = [
    # Client
    "ACNClient",
    "ACNError",
    "ACNRealtime",
    "ACNRealtimeOptions",
    "AuthMode",
    "WSState",
    # Regions (ADR-0013)
    "ACN_HOSTED_URLS",
    "hosted_base_url",
    "normalize_base_url",
    "resolve_hosted_base_url",
    # Agent models
    "AgentInfo",
    "AgentJoinRequest",
    "AgentJoinResponse",
    "AgentRegisterRequest",
    "AgentSearchOptions",
    "AgentStatus",
    # Communication models
    "AttentionFee",
    "BroadcastRequest",
    "BroadcastStrategy",
    "CommunicationProfile",
    "ManifestContentResponse",
    "ManifestEntry",
    "ManifestSendRequest",
    "MessageType",
    "SendMessageRequest",
    # Session models
    "SessionEntry",
    "SessionInviteRequest",
    # Subnet models
    "SubnetInfo",
    # Communication constants
    "KNOWN_INBOX_MESSAGE_STATUSES",
    # Payment models
    "KNOWN_PAYMENT_TASK_STATUSES",
    "PaymentCapability",
    "PaymentMethod",
    "PaymentNetwork",
    "PaymentTask",
    # Task models
    "TaskInfo",
    "TaskCreateRequest",
    "TaskAcceptRequest",
    "TaskAcceptResponse",
    "TaskSubmitRequest",
    "TaskReviewRequest",
    "ParticipationInfo",
]
