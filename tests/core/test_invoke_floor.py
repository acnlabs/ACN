"""invoke_floor_credits listing on the agent record."""

from acn.core.entities import Agent


def test_floor_round_trip_none_and_zero_and_int():
    bare = Agent(agent_id="a1", name="A", owner="o", endpoint="https://e.example")
    assert bare.invoke_floor_credits is None
    assert bare.to_dict()["invoke_floor_credits"] is None

    free = Agent.from_dict({**bare.to_dict(), "invoke_floor_credits": 0})
    assert free.invoke_floor_credits == 0

    listed = Agent.from_dict({**bare.to_dict(), "invoke_floor_credits": "50"})
    assert listed.invoke_floor_credits == 50
    assert listed.to_dict()["invoke_floor_credits"] == 50

    junk = Agent.from_dict({**bare.to_dict(), "invoke_floor_credits": "nope"})
    assert junk.invoke_floor_credits is None

    # bool is a subclass of int — must not become 1
    assert Agent.from_dict({**bare.to_dict(), "invoke_floor_credits": True}).invoke_floor_credits is None
    assert Agent.from_dict({**bare.to_dict(), "invoke_floor_credits": 12.0}).invoke_floor_credits == 12
    assert Agent.from_dict({**bare.to_dict(), "invoke_floor_credits": ["x"]}).invoke_floor_credits is None


def test_optional_int_redis_helper():
    from acn.infrastructure.persistence.redis.agent_repository import _optional_int

    assert _optional_int(None) is None
    assert _optional_int("") is None
    assert _optional_int(True) is None
    assert _optional_int("nope") is None
    assert _optional_int("50") == 50
    assert _optional_int(0) == 0
