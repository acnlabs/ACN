"""Regression tests for PostgreSQL ARRAY containment queries.

Railway runs against SQLAlchemy 2.0.49, where the base ``sqlalchemy.ARRAY``
type does not implement ``.contains()``. The ORM model must use
``sqlalchemy.dialects.postgresql.ARRAY`` so agent search paths can compile
``skills @> ARRAY[...]`` instead of raising at runtime.
"""

from sqlalchemy import String, cast, select
from sqlalchemy.dialects.postgresql import ARRAY, dialect

from acn.infrastructure.persistence.postgres.models import AgentModel


def test_agent_tags_contains_compiles_with_postgres_array_operator():
    stmt = select(AgentModel).where(
        AgentModel.tags.contains(cast(["coding"], ARRAY(String)))
    )

    compiled = str(stmt.compile(dialect=dialect()))

    assert "skills @> CAST(" in compiled


def test_agent_subnet_ids_contains_compiles_with_postgres_array_operator():
    stmt = select(AgentModel).where(
        AgentModel.subnet_ids.contains(cast(["public"], ARRAY(String)))
    )

    compiled = str(stmt.compile(dialect=dialect()))

    assert "subnet_ids @> CAST(" in compiled
