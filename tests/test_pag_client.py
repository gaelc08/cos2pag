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


TEMPLATE_PARTITION = {
    "uuid": "template-uuid",
    "name": "costotape",
    "writeProtected": False,
    "storageClassTypes": ["Standard"],
    "devType": "Tape",
    "devCodeRate": "None",
    "devAllocStrat": "RoundRobin",
    "devAllocThreshold": 10,
    "devAllocParallelism": 1,
    "devAllocMediaPref": "Any",
    "devAllocMediaAlt": "Any",
    "devAllocSrcPrio": 0,
    "devAllocSrcLimit": ["dev-uuid-1", "dev-uuid-2"],
    "cryptLocked": False,
    "cryptMode": "Off",
    "cryptAlgo": "None",
    "pstBufCfgPresent": False,
    "optionForceHighAvailability": False,
    "optionAllowReducedRedundancy": False,
    # fields that must NOT be copied over:
    "index": 1,
    "cTime": "2020-01-01T00:00:00Z",
    "mTime": "2020-01-01T00:00:00Z",
    "pvaGroupInformation": {"something": "irrelevant"},
}


def make_partition_client(monkeypatch, existing_tenant=None, template=TEMPLATE_PARTITION):
    client = PagClient.__new__(PagClient)
    partitions = [template] + ([existing_tenant] if existing_tenant else [])
    monkeypatch.setattr(client, "list_partitions", lambda: partitions)
    monkeypatch.setattr(client, "create_partition", MagicMock(return_value={"uuid": "new-part-uuid", "name": "ctie-001"}))
    return client


def test_ensure_partition_returns_existing_when_found(monkeypatch):
    existing = {"uuid": "tenant-uuid", "name": "ctie-001"}
    client = make_partition_client(monkeypatch, existing_tenant=existing)

    partition, action = client.ensure_partition("ctie-001", "costotape")

    assert action == "found"
    assert partition is existing
    client.create_partition.assert_not_called()


def test_ensure_partition_clones_template_fields_when_creating(monkeypatch):
    client = make_partition_client(monkeypatch, existing_tenant=None)

    partition, action = client.ensure_partition("ctie-001", "costotape")

    assert action == "created"
    client.create_partition.assert_called_once()
    (body,), kwargs = client.create_partition.call_args
    assert body["name"] == "ctie-001"
    assert body["devType"] == "Tape"
    assert body["devAllocSrcLimit"] == ["dev-uuid-1", "dev-uuid-2"]
    assert body["storageClassTypes"] == ["Standard"]
    # server-assigned / informational fields must not be copied
    assert "uuid" not in body
    assert "index" not in body
    assert "cTime" not in body
    assert "pvaGroupInformation" not in body
    assert kwargs.get("encryption_password") is None


def test_ensure_partition_passes_encryption_password_only_for_privkey_template(monkeypatch):
    privkey_template = {**TEMPLATE_PARTITION, "cryptMode": "PrivKey"}
    client = make_partition_client(monkeypatch, existing_tenant=None, template=privkey_template)

    client.ensure_partition("ctie-001", "costotape", encryption_password="s3cr3t")

    (_body,), kwargs = client.create_partition.call_args
    assert kwargs.get("encryption_password") == "s3cr3t"


def test_ensure_partition_raises_when_template_missing(monkeypatch):
    client = PagClient.__new__(PagClient)
    monkeypatch.setattr(client, "list_partitions", lambda: [])

    try:
        client.ensure_partition("ctie-001", "costotape")
        assert False, "expected LookupError"
    except LookupError:
        pass
