from unittest.mock import MagicMock

from cos2pag.pag_client import PagClient

PARTITION_UUID = "part-uuid"
BUCKET = "my-bucket"
OWNER = {"uuid": "pdr-uuid", "type": "User"}


def make_client(monkeypatch, existing_repo=None):
    client = PagClient.__new__(PagClient)  # skip __init__, no real session needed
    monkeypatch.setattr(client, "find_repository", lambda partition_uuid, name: existing_repo)
    monkeypatch.setattr(client, "create_repository", MagicMock(return_value={"uuid": "new-uuid"}))
    monkeypatch.setattr(client, "update_repository", MagicMock(return_value={"uuid": "existing-uuid"}))
    return client


def test_ensure_repository_creates_when_missing(monkeypatch):
    client = make_client(monkeypatch, existing_repo=None)

    repo, action = client.ensure_repository(PARTITION_UUID, BUCKET, OWNER)

    assert action == "created"
    client.create_repository.assert_called_once()
    client.update_repository.assert_not_called()


def test_ensure_repository_skips_patch_when_owner_already_matches(monkeypatch):
    existing = {"uuid": "existing-uuid", "name": BUCKET, "owner": {"uuid": "pdr-uuid", "type": "User"}}
    client = make_client(monkeypatch, existing_repo=existing)

    repo, action = client.ensure_repository(PARTITION_UUID, BUCKET, OWNER)

    assert action == "unchanged"
    assert repo is existing
    client.create_repository.assert_not_called()
    client.update_repository.assert_not_called()


def test_ensure_repository_patches_when_owner_differs(monkeypatch):
    existing = {"uuid": "existing-uuid", "name": BUCKET, "owner": {"uuid": "someone-else", "type": "User"}}
    client = make_client(monkeypatch, existing_repo=existing)

    repo, action = client.ensure_repository(PARTITION_UUID, BUCKET, OWNER)

    assert action == "updated"
    client.update_repository.assert_called_once()
    client.create_repository.assert_not_called()


def test_ensure_repository_patches_when_sosapi_flag_differs(monkeypatch):
    existing = {
        "uuid": "existing-uuid",
        "name": BUCKET,
        "owner": {"uuid": "pdr-uuid", "type": "User"},
        "sosapiEnabled": False,
    }
    client = make_client(monkeypatch, existing_repo=existing)

    repo, action = client.ensure_repository(PARTITION_UUID, BUCKET, OWNER, sosapi_enabled=True)

    assert action == "updated"
    client.update_repository.assert_called_once()
