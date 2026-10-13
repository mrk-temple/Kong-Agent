import pytest

from kong.memory import Authority, Capability, MemoryService, Scope


def authority(input_id="input-1", *, owner_id="owner-fixture", profile_id="kong-fixture", scopes=None,
              actor="owner", capabilities=None):
    return Authority(owner_id=owner_id, profile_id=profile_id, actor=actor,
                     input_id=input_id, scopes=(Scope(),) if scopes is None else scopes,
                     capabilities=frozenset(Capability) if capabilities is None else frozenset(capabilities))


@pytest.fixture
def auth():
    return authority()


@pytest.fixture
def core(tmp_path, auth):
    with MemoryService(tmp_path / "memory.sqlite3") as service:
        service.create_profile(auth, command_id="create-profile")
        yield service
