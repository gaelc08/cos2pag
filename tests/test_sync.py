from unittest.mock import MagicMock

import pytest

from cos2pag import sync as sync_module
from cos2pag.http_client import ApiError
from cos2pag.sync import sync_bucket

BUCKET = "my-bucket"


def base_config():
    return {
        "cos": {
            "base_url": "https://cos.example.com:8338",
            "auth": {"type": "bearer", "token": "t"},
            "container_vaults": {
                "CV1": {"backup_service_account": {"grantee": "backup-sa", "permission": "READ"}},
            },
            "pdr_ip": "203.0.113.10/32",
        },
        "pag": {
            "base_url": "https://pag.example.com",
            "auth": {"type": "basic", "username": "u", "password": "p"},
            "partition": "PART-01",
            "owner": {"uuid": "pdr-user-uuid", "type": "User"},
        },
        "pdr": {
            "base_url": "https://pdr.example.com",
            "auth": {"type": "basic", "username": "u", "password": "p"},
            "source_s3": {"server_url": "https://cos.example.com:8338", "access_key": "ak", "secret_key": "sk"},
            "target_s3": {"server_url": "https://pag.example.com:8443", "access_key": "ak2", "secret_key": "sk2"},
        },
    }


@pytest.fixture
def fake_clients(monkeypatch):
    cos_client = MagicMock()
    cos_client.get_bucket.return_value = {
        "acl": {"existing-user": ["READ"]},
        "firewall": {"allowed_ip": ["192.168.1.0/24"]},
        "notifications": {},
        "time_updated": "2024-01-01T00:00:00Z",
        "storage_location": "CV1",
    }

    pag_client = MagicMock()
    pag_client.find_partition.return_value = {"uuid": "part-uuid", "name": "PART-01"}
    pag_client.ensure_repository.return_value = ({"uuid": "repo-uuid", "name": BUCKET}, True)

    pdr_client = MagicMock()
    pdr_client.ensure_task.return_value = ({"id": 42, "alias": BUCKET}, True)

    monkeypatch.setattr(sync_module, "_build_cos_client", lambda cfg, dry_run: cos_client)
    monkeypatch.setattr(sync_module, "_build_pag_client", lambda cfg, dry_run: pag_client)
    monkeypatch.setattr(sync_module, "_build_pdr_client", lambda cfg, dry_run: pdr_client)

    return {"cos": cos_client, "pag": pag_client, "pdr": pdr_client}


def test_sync_bucket_full_happy_path(fake_clients):
    report = sync_bucket(base_config(), BUCKET)

    cos_client = fake_clients["cos"]
    patch_calls = cos_client.patch_bucket.call_args_list
    assert len(patch_calls) == 2

    acl_firewall_call = patch_calls[0]
    body = acl_firewall_call.args[1]
    assert {"grantee": "backup-sa", "permission": "READ"} in body["acl"]
    assert {"grantee": "existing-user", "permission": "READ"} in body["acl"]
    assert body["firewall"]["allowed_ip"] == ["192.168.1.0/24", "203.0.113.10/32"]

    notif_call = patch_calls[1]
    assert notif_call.args[1] == {"notifications": {"topic": BUCKET}}

    pag_client = fake_clients["pag"]
    pag_client.ensure_repository.assert_called_once()
    args, kwargs = pag_client.ensure_repository.call_args
    assert args[0] == "part-uuid"
    assert args[1] == BUCKET
    assert args[2] == {"uuid": "pdr-user-uuid", "type": "User"}

    pdr_client = fake_clients["pdr"]
    pdr_client.ensure_task.assert_called_once()
    (task_body,) = pdr_client.ensure_task.call_args.args
    assert task_body["alias"] == BUCKET
    assert task_body["source"]["s3"]["bucketName"] == BUCKET
    assert task_body["source"]["s3"]["serverURL"] == "https://cos.example.com:8338"
    assert task_body["target"]["s3"]["bucketName"] == BUCKET
    assert task_body["target"]["s3"]["serverURL"] == "https://pag.example.com:8443"

    step_names = [s.name for s in report.steps]
    assert "cos.acl_and_firewall" in step_names
    assert "cos.notifications" in step_names
    assert "pag.repository" in step_names
    assert "pdr.task" in step_names


def test_sync_bucket_skips_cos_patch_when_already_up_to_date(fake_clients):
    fake_clients["cos"].get_bucket.return_value = {
        "acl": {"backup-sa": ["READ"]},
        "firewall": {"allowed_ip": ["203.0.113.10/32"]},
        "notifications": {"topic": BUCKET},
        "time_updated": "2024-01-01T00:00:00Z",
        "storage_location": "CV1",
    }

    report = sync_bucket(base_config(), BUCKET)

    fake_clients["cos"].patch_bucket.assert_not_called()
    step = next(s for s in report.steps if s.name == "cos.acl_and_firewall")
    assert step.skipped is True


def test_sync_bucket_reuses_existing_pdr_task(fake_clients):
    fake_clients["pdr"].ensure_task.return_value = ({"id": 42, "alias": BUCKET}, False)
    report = sync_bucket(base_config(), BUCKET)
    step = next(s for s in report.steps if s.name == "pdr.task")
    assert step.skipped is True
    assert step.changed is False


def test_sync_bucket_tolerates_notifications_rejected_by_cos(fake_clients):
    fake_clients["cos"].patch_bucket.side_effect = [
        None,
        ApiError("PATCH", "https://cos.example.com/container/x", 400, "MalformedNotificationsError"),
    ]

    report = sync_bucket(base_config(), BUCKET)

    step = next(s for s in report.steps if s.name == "cos.notifications")
    assert step.skipped is True


def test_sync_bucket_refuses_to_create_whitelist_from_scratch(fake_clients):
    fake_clients["cos"].get_bucket.return_value = {
        "acl": {"backup-sa": ["READ"]},
        "firewall": {},
        "notifications": {"topic": BUCKET},
        "time_updated": "2024-01-01T00:00:00Z",
        "storage_location": "CV1",
    }

    report = sync_bucket(base_config(), BUCKET)

    fake_clients["cos"].patch_bucket.assert_not_called()
    step = next(s for s in report.steps if s.name == "cos.firewall")
    assert step.skipped is True


def test_sync_bucket_picks_backup_sa_by_container_vault(fake_clients):
    config = base_config()
    config["cos"]["container_vaults"]["CV2"] = {
        "backup_service_account": {"grantee": "backup-sa-cv2", "permission": "READ"}
    }
    fake_clients["cos"].get_bucket.return_value = {
        "acl": {},
        "firewall": {"allowed_ip": ["192.168.1.0/24"]},
        "notifications": {},
        "time_updated": "2024-01-01T00:00:00Z",
        "storage_location": "CV2",
    }

    sync_bucket(config, BUCKET)

    body = fake_clients["cos"].patch_bucket.call_args_list[0].args[1]
    assert {"grantee": "backup-sa-cv2", "permission": "READ"} in body["acl"]
    assert {"grantee": "backup-sa", "permission": "READ"} not in body["acl"]


def test_sync_bucket_fails_clearly_for_unknown_container_vault(fake_clients):
    from cos2pag.config import ConfigError

    fake_clients["cos"].get_bucket.return_value = {
        "acl": {},
        "firewall": {"allowed_ip": ["192.168.1.0/24"]},
        "notifications": {},
        "time_updated": "2024-01-01T00:00:00Z",
        "storage_location": "UNKNOWN-CV",
    }

    with pytest.raises(ConfigError):
        sync_bucket(base_config(), BUCKET)
