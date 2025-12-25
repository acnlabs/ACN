"""FastAPI Dependencies for ACN

Provides dependency injection for core services.
"""

from typing import Annotated

from fastapi import Depends

from ..auth.middleware import get_subject
from ..config import get_settings
from ..infrastructure.messaging import (
    BroadcastService,
    MessageRouter,
    SubnetManager,
    WebSocketManager,
)
from ..infrastructure.persistence.redis.registry import AgentRegistry
from ..monitoring import Analytics, AuditLogger, MetricsCollector
from ..protocols.ap2 import PaymentDiscoveryService, PaymentTaskManager, WebhookService
from ..services import AgentService, MessageService, SubnetService

settings = get_settings()

# Global service instances (initialized in lifespan)
_registry: AgentRegistry | None = None
_agent_service: AgentService | None = None
_message_service: MessageService | None = None
_subnet_service: SubnetService | None = None
_router: MessageRouter | None = None
_broadcast: BroadcastService | None = None
_ws_manager: WebSocketManager | None = None
_subnet_manager: SubnetManager | None = None
_metrics: MetricsCollector | None = None
_audit: AuditLogger | None = None
_analytics: Analytics | None = None
_payment_discovery: PaymentDiscoveryService | None = None
_payment_tasks: PaymentTaskManager | None = None
_webhook_service: WebhookService | None = None


def init_services(
    registry: AgentRegistry,
    agent_service: AgentService,
    message_service: MessageService,
    subnet_service: SubnetService,
    router: MessageRouter,
    broadcast: BroadcastService,
    ws_manager: WebSocketManager,
    subnet_manager: SubnetManager,
    metrics: MetricsCollector,
    audit: AuditLogger,
    analytics: Analytics,
    payment_discovery: PaymentDiscoveryService,
    payment_tasks: PaymentTaskManager,
    webhook_service: WebhookService,
) -> None:
    """Initialize global service instances (called from lifespan)"""
    global \
        _registry, \
        _agent_service, \
        _message_service, \
        _subnet_service, \
        _router, \
        _broadcast, \
        _ws_manager, \
        _subnet_manager
    global _metrics, _audit, _analytics
    global _payment_discovery, _payment_tasks, _webhook_service

    _registry = registry
    _agent_service = agent_service
    _message_service = message_service
    _subnet_service = subnet_service
    _router = router
    _broadcast = broadcast
    _ws_manager = ws_manager
    _subnet_manager = subnet_manager
    _metrics = metrics
    _audit = audit
    _analytics = analytics
    _payment_discovery = payment_discovery
    _payment_tasks = payment_tasks
    _webhook_service = webhook_service


# Dependency functions
def get_registry() -> AgentRegistry:
    """Get AgentRegistry instance"""
    if _registry is None:
        raise RuntimeError("AgentRegistry not initialized")
    return _registry


def get_agent_service() -> AgentService:
    """Get AgentService instance"""
    if _agent_service is None:
        raise RuntimeError("AgentService not initialized")
    return _agent_service


def get_message_service() -> MessageService:
    """Get MessageService instance"""
    if _message_service is None:
        raise RuntimeError("MessageService not initialized")
    return _message_service


def get_subnet_service() -> SubnetService:
    """Get SubnetService instance"""
    if _subnet_service is None:
        raise RuntimeError("SubnetService not initialized")
    return _subnet_service


def get_router() -> MessageRouter:
    """Get MessageRouter instance"""
    if _router is None:
        raise RuntimeError("MessageRouter not initialized")
    return _router


def get_broadcast() -> BroadcastService:
    """Get BroadcastService instance"""
    if _broadcast is None:
        raise RuntimeError("BroadcastService not initialized")
    return _broadcast


def get_ws_manager() -> WebSocketManager:
    """Get WebSocketManager instance"""
    if _ws_manager is None:
        raise RuntimeError("WebSocketManager not initialized")
    return _ws_manager


def get_subnet_manager() -> SubnetManager:
    """Get SubnetManager instance"""
    if _subnet_manager is None:
        raise RuntimeError("SubnetManager not initialized")
    return _subnet_manager


def get_metrics() -> MetricsCollector:
    """Get MetricsCollector instance"""
    if _metrics is None:
        raise RuntimeError("MetricsCollector not initialized")
    return _metrics


def get_audit() -> AuditLogger:
    """Get AuditLogger instance"""
    if _audit is None:
        raise RuntimeError("AuditLogger not initialized")
    return _audit


def get_analytics() -> Analytics:
    """Get Analytics instance"""
    if _analytics is None:
        raise RuntimeError("Analytics not initialized")
    return _analytics


def get_payment_discovery() -> PaymentDiscoveryService:
    """Get PaymentDiscoveryService instance"""
    if _payment_discovery is None:
        raise RuntimeError("PaymentDiscoveryService not initialized")
    return _payment_discovery


def get_payment_tasks() -> PaymentTaskManager:
    """Get PaymentTaskManager instance"""
    if _payment_tasks is None:
        raise RuntimeError("PaymentTaskManager not initialized")
    return _payment_tasks


def get_webhook_service() -> WebhookService:
    """Get WebhookService instance"""
    if _webhook_service is None:
        raise RuntimeError("WebhookService not initialized")
    return _webhook_service


# Type aliases for cleaner dependency injection
RegistryDep = Annotated[AgentRegistry, Depends(get_registry)]
AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]
MessageServiceDep = Annotated[MessageService, Depends(get_message_service)]
SubnetServiceDep = Annotated[SubnetService, Depends(get_subnet_service)]
RouterDep = Annotated[MessageRouter, Depends(get_router)]
BroadcastDep = Annotated[BroadcastService, Depends(get_broadcast)]
SubnetManagerDep = Annotated[SubnetManager, Depends(get_subnet_manager)]
WsManagerDep = Annotated[WebSocketManager, Depends(get_ws_manager)]
MetricsDep = Annotated[MetricsCollector, Depends(get_metrics)]
AuditDep = Annotated[AuditLogger, Depends(get_audit)]
AnalyticsDep = Annotated[Analytics, Depends(get_analytics)]
PaymentDiscoveryDep = Annotated[PaymentDiscoveryService, Depends(get_payment_discovery)]
PaymentTasksDep = Annotated[PaymentTaskManager, Depends(get_payment_tasks)]
WebhookServiceDep = Annotated[WebhookService, Depends(get_webhook_service)]

# Auth dependencies
SubjectDep = Annotated[str, Depends(get_subject)]
