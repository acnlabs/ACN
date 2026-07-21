"""PostgresOrgRepository regressions — unique-fence conflict mapping.

``uq_orgs_subnet_id`` (alembic ``e4f5a6b7c8d9``) enforces one Org per
subnet at the database level; ``save_org`` must translate that
IntegrityError into the domain ``OrgSubnetBindingConflictError`` so a
create that loses the pre-check race surfaces as a 409, not a bare 500.

Mock-session style mirrors ``test_postgres_subnet_repository_nesting.py``
— no live PostgreSQL needed; schema correctness lives in alembic.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from acn.core.entities.org import Org, OrgPrincipal
from acn.core.exceptions import OrgSubnetBindingConflictError
from acn.infrastructure.persistence.postgres.org_repository import (
    PostgresOrgRepository,
)

pytestmark = pytest.mark.asyncio


def _make_session_factory():
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    factory = MagicMock(return_value=session)
    return factory, session


def _org(org_id: str = "org_a", subnet_id: str = "fence-1") -> Org:
    return Org(
        org_id=org_id,
        display_name="Test",
        created_by=OrgPrincipal(kind="agent", subject="agt_steward"),
        subnet_id=subnet_id,
        steward_agent_id="agt_steward",
    )


def _integrity_error(message: str) -> IntegrityError:
    return IntegrityError("INSERT INTO orgs ...", {}, Exception(message))


async def test_unique_subnet_violation_maps_to_domain_conflict():
    factory, session = _make_session_factory()
    session.get.return_value = None  # INSERT branch
    session.commit.side_effect = _integrity_error(
        'duplicate key value violates unique constraint "uq_orgs_subnet_id"'
    )
    holder_result = MagicMock()
    holder_result.scalar_one_or_none.return_value = "org_winner"
    session.execute.return_value = holder_result

    repo = PostgresOrgRepository(factory)
    with pytest.raises(OrgSubnetBindingConflictError) as ei:
        await repo.save_org(_org("org_loser"))

    assert ei.value.subnet_id == "fence-1"
    assert ei.value.bound_org_id == "org_winner"
    session.rollback.assert_awaited_once()


async def test_unrelated_integrity_error_is_not_swallowed():
    factory, session = _make_session_factory()
    session.get.return_value = None
    session.commit.side_effect = _integrity_error(
        'null value in column "display_name" violates not-null constraint'
    )

    repo = PostgresOrgRepository(factory)
    with pytest.raises(IntegrityError):
        await repo.save_org(_org())
