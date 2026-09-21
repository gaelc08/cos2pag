from unittest.mock import MagicMock

import pytest

from cos2pag import sync as sync_module
from cos2pag.http_client import ApiError
from cos2pag.sync import derive_tenant, sync_bucket

BUCKET = "my-bucket"


def test_derive_tenant_takes_leading_hyphen_segments():
    assert derive_tenant("ctie-001-gael-nextcloud", 2) == "ctie-001"
    assert derive_tenant("ctie-hive-prd-acdi", 2) == "ctie-hive"


def test_derive_tenant_one_part():
    assert derive_tenant("ctie-001-gael-nextcloud", 1) == "ctie"


def test_derive_tenant_falls_back_to_whole_name_when_too_short():
    assert derive_tenant("test", 2) == "test"


def base_config():
    return {
        "cos": {
            "base_url": "https://cos.example.com:8338",
            "auth": {"type": "bearer", "token": "t"},
            "backup_service_account": {"grantee": "backup-sa", "permission": "READ"},
            "pdr_ip": "203.0.113.10/32",
        },
        "pag": {
            "base_url": "https://pag.example.com",
            "auth": {"type": "basic", "username": "u", "password": "p"},
            "new_partition_defaults": {"devType": "Tape", "storageClassTypes": ["Standard"]},
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
    }

    pag_client = MagicMock()
    pag_client.ensure_partition.return_value = ({"uuid": "part-uuid", "name": "my-bucket"}, "found")
    pag_client.ensure_repository.return_value = ({"uuid": "repo-uuid", "name": BUCKET}, "created")

    pdr_client = MagicMock()
    pdr_client.ensure_task.return_value = ({"id": 42, "alias": BUCKET}, "created")

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
    assert "pag.partition" in step_names
    assert "pag.repository" in step_names
    assert "pdr.task" in step_names


def test_sync_bucket_skips_cos_patch_when_already_up_to_date(fake_clients):
    fake_clients["cos"].get_bucket.return_value = {
        "acl": {"backup-sa": ["READ"]},
        "firewall": {"allowed_ip": ["203.0.113.10/32"]},
        "notifications": {"topic": BUCKET},
        "time_updated": "2024-01-01T00:00:00Z",
    }

    report = sync_bucket(base_config(), BUCKET)

    fake_clients["cos"].patch_bucket.assert_not_called()
    step = next(s for s in report.steps if s.name == "cos.acl_and_firewall")
    assert step.skipped is True


def test_sync_bucket_reuses_existing_pdr_task(fake_clients):
    fake_clients["pdr"].ensure_task.return_value = ({"id": 42, "alias": BUCKET}, "unchanged")
    report = sync_bucket(base_config(), BUCKET)
    step = next(s for s in report.steps if s.name == "pdr.task")
    assert step.skipped is True
    assert step.changed is False


def test_sync_bucket_reports_updated_pdr_task_as_changed(fake_clients):
    fake_clients["pdr"].ensure_task.return_value = ({"id": 42, "alias": BUCKET}, "updated")
    report = sync_bucket(base_config(), BUCKET)
    step = next(s for s in report.steps if s.name == "pdr.task")
    assert step.skipped is False
    assert step.changed is True


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
    }

    report = sync_bucket(base_config(), BUCKET)

    fake_clients["cos"].patch_bucket.assert_not_called()
    step = next(s for s in report.steps if s.name == "cos.firewall")
    assert step.skipped is True


def test_sync_bucket_skips_pag_patch_when_repository_unchanged(fake_clients):
    fake_clients["pag"].ensure_repository.return_value = (
        {"uuid": "repo-uuid", "name": BUCKET},
        "unchanged",
    )

    report = sync_bucket(base_config(), BUCKET)

    step = next(s for s in report.steps if s.name == "pag.repository")
    assert step.skipped is True
    assert step.changed is False


def test_sync_pdr_task_includes_max_error_retry_when_configured(fake_clients):
    config = base_config()
    config["pdr"]["source_s3"]["max_error_retry"] = 2
    config["pdr"]["target_s3"]["max_error_retry"] = 2

    sync_bucket(config, BUCKET)

    (task_body,) = fake_clients["pdr"].ensure_task.call_args.args
    assert task_body["source"]["maxErrorRetry"] == 2
    assert task_body["target"]["maxErrorRetry"] == 2


def test_sync_pdr_task_omits_max_error_retry_when_not_configured(fake_clients):
    sync_bucket(base_config(), BUCKET)

    (task_body,) = fake_clients["pdr"].ensure_task.call_args.args
    assert "maxErrorRetry" not in task_body["source"]
    assert "maxErrorRetry" not in task_body["target"]


def test_sync_pdr_task_includes_kafka_notifications_defaulting_topic_to_bucket_name(fake_clients):
    config = base_config()
    config["pdr"]["notifications"] = {
        "enabled": True,
        "type": "kafka",
        "kafka": {
            "schema": "IBMCOS",
            "server_url": "thoth-1:9092,thoth-2:9092",
            "connection_type": "None",
        },
    }

    sync_bucket(config, BUCKET)

    (task_body,) = fake_clients["pdr"].ensure_task.call_args.args
    assert task_body["notifications"] == {
        "enabled": True,
        "type": "kafka",
        "kafka": {
            "schema": "IBMCOS",
            "serverURL": "thoth-1:9092,thoth-2:9092",
            "connectionType": "None",
            "topic": BUCKET,
        },
    }


def test_sync_pdr_task_notifications_topic_can_be_overridden(fake_clients):
    config = base_config()
    config["pdr"]["notifications"] = {
        "enabled": True,
        "type": "kafka",
        "kafka": {"topic": "fixed-topic"},
    }

    sync_bucket(config, BUCKET)

    (task_body,) = fake_clients["pdr"].ensure_task.call_args.args
    assert task_body["notifications"]["kafka"]["topic"] == "fixed-topic"


def test_sync_pdr_task_omits_notifications_when_disabled(fake_clients):
    config = base_config()
    config["pdr"]["notifications"] = {"enabled": False}

    sync_bucket(config, BUCKET)

    (task_body,) = fake_clients["pdr"].ensure_task.call_args.args
    assert "notifications" not in task_body


def test_sync_bucket_derives_tenant_from_bucket_prefix(fake_clients):
    sync_bucket(base_config(), "ctie-001-gael-nextcloud")

    fake_clients["pag"].ensure_partition.assert_called_once()
    args, kwargs = fake_clients["pag"].ensure_partition.call_args
    assert args[0] == "ctie-001"
    assert args[1] == {"devType": "Tape", "storageClassTypes": ["Standard"]}


def test_sync_bucket_tenant_prefix_parts_is_configurable(fake_clients):
    config = base_config()
    config["pag"]["tenant_prefix_parts"] = 1

    sync_bucket(config, "ctie-001-gael-nextcloud")

    args, kwargs = fake_clients["pag"].ensure_partition.call_args
    assert args[0] == "ctie"


def test_sync_bucket_explicit_tenant_overrides_derivation(fake_clients):
    sync_bucket(base_config(), "ctie-001-gael-nextcloud", tenant="forced-tenant")

    args, kwargs = fake_clients["pag"].ensure_partition.call_args
    assert args[0] == "forced-tenant"


def test_sync_bucket_reports_partition_creation_as_changed(fake_clients):
    fake_clients["pag"].ensure_partition.return_value = (
        {"uuid": "new-part-uuid", "name": "ctie-001"},
        "created",
    )

    report = sync_bucket(base_config(), BUCKET)

    step = next(s for s in report.steps if s.name == "pag.partition")
    assert step.changed is True
    assert step.skipped is False


def test_sync_bucket_reports_existing_partition_as_skipped(fake_clients):
    report = sync_bucket(base_config(), BUCKET)  # fixture default action is "found"

    step = next(s for s in report.steps if s.name == "pag.partition")
    assert step.changed is False
    assert step.skipped is True


def test_sync_bucket_handles_dry_run_partition_creation_without_uuid(fake_clients):
    fake_clients["pag"].ensure_partition.return_value = (None, "created")

    report = sync_bucket(base_config(), BUCKET, dry_run=True)

    fake_clients["pag"].ensure_repository.assert_not_called()
    repo_step = next(s for s in report.steps if s.name == "pag.repository")
    assert repo_step.changed is True
