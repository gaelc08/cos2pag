"""Orchestrates a COS -> PAG (via PDR) bucket replication setup.

Pipeline for a given bucket name, matching the 4 stages of the task:

1. COS:  read the bucket's current ACL + IP whitelist, then PATCH the same
   bucket to add the backup service account to the ACL and the PDR IP to
   the whitelist, then PATCH the bucket's notification topic to the
   bucket's own name.
2. PAG:  create (or reuse) an object repository with the same name as the
   COS bucket, in a specific partition, owned by the PDR user/ESP.
3. PDR:  create the replication task (COS bucket -> PAG repository, both
   exposed as S3 endpoints).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .config import AuthConfig, ConfigError, require
from .cos_client import CosClient, acl_map_to_pairs, merge_acl, merge_allowed_ip
from .http_client import ApiError, build_session
from .pag_client import PagClient
from .pdr_client import PdrClient

logger = logging.getLogger(__name__)


@dataclass
class StepResult:
    name: str
    changed: bool
    detail: str = ""
    skipped: bool = False


@dataclass
class SyncReport:
    bucket_name: str
    dry_run: bool = False
    steps: list[StepResult] = field(default_factory=list)

    def add(self, name: str, changed: bool, detail: str = "", skipped: bool = False) -> None:
        if changed and self.dry_run:
            detail = f"{detail} [dry-run, no request sent]".strip()
        self.steps.append(StepResult(name=name, changed=changed, detail=detail, skipped=skipped))
        level = logging.INFO if not skipped else logging.WARNING
        logger.log(level, "[%s] %s%s%s", self.bucket_name, name, " (skipped)" if skipped else "", f" - {detail}" if detail else "")


def _build_cos_client(cfg: dict, dry_run: bool) -> CosClient:
    base_url = require(cfg, "base_url", "cos")
    auth = AuthConfig.from_dict(cfg.get("auth", {}))
    session = build_session(auth, verify_ssl=cfg.get("verify_ssl", True))
    return CosClient(base_url, session, timeout=cfg.get("timeout", 30), dry_run=dry_run)


def _build_pag_client(cfg: dict, dry_run: bool) -> PagClient:
    base_url = require(cfg, "base_url", "pag")
    auth = AuthConfig.from_dict(cfg.get("auth", {}))
    session = build_session(auth, verify_ssl=cfg.get("verify_ssl", True))
    return PagClient(base_url, session, timeout=cfg.get("timeout", 30), dry_run=dry_run)


def _build_pdr_client(cfg: dict, dry_run: bool) -> PdrClient:
    base_url = require(cfg, "base_url", "pdr")
    auth = AuthConfig.from_dict(cfg.get("auth", {}))
    session = build_session(auth, verify_ssl=cfg.get("verify_ssl", True))
    return PdrClient(base_url, session, timeout=cfg.get("timeout", 30), dry_run=dry_run)


def _sync_cos_bucket(cos_cfg: dict, bucket_name: str, dry_run: bool, report: SyncReport) -> None:
    client = _build_cos_client(cos_cfg, dry_run)

    bucket = client.get_bucket(bucket_name)
    existing_acl = bucket.get("acl")
    existing_firewall = bucket.get("firewall")
    time_updated = bucket.get("time_updated")

    backup_sa = require(cos_cfg, "backup_service_account", "cos")
    grantee = require(backup_sa, "grantee", "cos.backup_service_account")
    permission = backup_sa.get("permission", "READ")
    pdr_ip = require(cos_cfg, "pdr_ip", "cos")
    allow_create_whitelist = cos_cfg.get("allow_create_whitelist", False)

    new_acl_pairs = merge_acl(existing_acl, grantee, permission)
    acl_changed = new_acl_pairs != acl_map_to_pairs(existing_acl)

    new_allowed_ip, ip_changed = merge_allowed_ip(existing_firewall, pdr_ip, allow_create_whitelist)
    if new_allowed_ip is None and not ip_changed:
        report.add(
            "cos.firewall",
            changed=False,
            skipped=True,
            detail=(
                f"bucket '{bucket_name}' has no existing IP whitelist; refusing to create one with only "
                f"{pdr_ip} in it (would lock the bucket down). Set cos.allow_create_whitelist: true to force it."
            ),
        )

    patch_body: dict[str, Any] = {}
    if acl_changed:
        patch_body["acl"] = new_acl_pairs
    if ip_changed:
        patch_body["firewall"] = {"allowed_ip": new_allowed_ip}

    if patch_body:
        client.patch_bucket(bucket_name, patch_body, if_unmodified_since=time_updated)
        report.add(
            "cos.acl_and_firewall",
            changed=True,
            detail=f"acl_changed={acl_changed} ip_changed={ip_changed}",
        )
    else:
        report.add("cos.acl_and_firewall", changed=False, skipped=True, detail="already up to date")

    current_topic = (bucket.get("notifications") or {}).get("topic")
    if current_topic == bucket_name:
        report.add("cos.notifications", changed=False, skipped=True, detail="topic already set")
    else:
        try:
            client.patch_bucket(bucket_name, {"notifications": {"topic": bucket_name}})
            report.add("cos.notifications", changed=True, detail=f"topic={bucket_name}")
        except ApiError as exc:
            if exc.status_code == 400:
                report.add(
                    "cos.notifications",
                    changed=False,
                    skipped=True,
                    detail=(
                        "rejected by COS (likely the container vault is not assigned to a Notification "
                        f"Service): {exc}"
                    ),
                )
            else:
                raise


def _sync_pag_repository(pag_cfg: dict, bucket_name: str, tenant_name: str, dry_run: bool, report: SyncReport) -> dict | None:
    client = _build_pag_client(pag_cfg, dry_run)
    partition_defaults = require(pag_cfg, "new_partition_defaults", "pag")
    owner = require(pag_cfg, "owner", "pag")

    partition, partition_action = client.ensure_partition(
        tenant_name, partition_defaults, encryption_password=pag_cfg.get("partition_encryption_password")
    )
    report.add(
        "pag.partition",
        changed=(partition_action == "created"),
        skipped=(partition_action == "found"),
        detail=f"{partition_action} partition '{tenant_name}'",
    )

    if partition is None:
        # dry-run: the partition doesn't exist yet, so there's nothing
        # real to list/create a repository against.
        report.add(
            "pag.repository",
            changed=True,
            detail=f"created repository '{bucket_name}' in partition '{tenant_name}'",
        )
        return None

    repo, action = client.ensure_repository(
        partition["uuid"],
        bucket_name,
        owner,
        hidden=pag_cfg.get("hidden"),
        write_protected=pag_cfg.get("write_protected"),
        sosapi_enabled=pag_cfg.get("sosapi_enabled", False),
    )
    detail = f"{action} repository '{bucket_name}' in partition '{partition.get('name')}'"
    if action == "unchanged":
        report.add("pag.repository", changed=False, skipped=True, detail=detail)
    else:
        report.add("pag.repository", changed=True, detail=detail)
    return repo


def _sync_pdr_task(pdr_cfg: dict, bucket_name: str, dry_run: bool, report: SyncReport) -> None:
    client = _build_pdr_client(pdr_cfg, dry_run)

    source_s3_cfg = require(pdr_cfg, "source_s3", "pdr")
    target_s3_cfg = require(pdr_cfg, "target_s3", "pdr")

    def s3_options(s3_cfg: dict) -> dict:
        return {
            "bucketName": bucket_name,
            "serverURL": require(s3_cfg, "server_url", "pdr.source_s3/target_s3"),
            "accessKey": s3_cfg.get("access_key"),
            "secretKey": s3_cfg.get("secret_key"),
            "forcePathStyle": s3_cfg.get("force_path_style", True),
            "signatureVersion": s3_cfg.get("signature_version"),
            "prefix": s3_cfg.get("prefix"),
            "timeout": s3_cfg.get("timeout"),
        }

    def task_side(s3_cfg: dict) -> dict:
        # maxErrorRetry lives on the task side (source/target), as a
        # sibling of "s3", not inside the s3 options block itself.
        side: dict[str, Any] = {"type": "s3", "s3": s3_options(s3_cfg)}
        if s3_cfg.get("max_error_retry") is not None:
            side["maxErrorRetry"] = s3_cfg["max_error_retry"]
        return side

    task_body: dict[str, Any] = {
        "alias": bucket_name,
        "source": task_side(source_s3_cfg),
        "target": task_side(target_s3_cfg),
    }
    if "copy_options" in pdr_cfg:
        co = pdr_cfg["copy_options"]
        task_body["options"] = {
            "modificationTime": co.get("modification_time"),
            "metadata": co.get("metadata"),
            "acl": co.get("acl"),
            "tags": co.get("tags"),
            "objectLock": co.get("object_lock"),
            "mpuPartSizeMB": co.get("mpu_part_size_mb"),
            "readInTapeOrder": co.get("read_in_tape_order"),
            "maxParallelJobs": co.get("max_parallel_jobs"),
            "checkDestination": co.get("check_destination"),
            "enableDeletion": co.get("enable_deletion"),
            "deleteDelayDays": co.get("delete_delay_days"),
        }
    if "schedule" in pdr_cfg:
        sch = pdr_cfg["schedule"]
        task_body["schedule"] = {
            "enabled": sch.get("enabled", False),
            "weekMultiplier": sch.get("week_multiplier"),
            "dowMask": sch.get("dow_mask"),
            "hour": sch.get("hour"),
        }
    if pdr_cfg.get("notifications", {}).get("enabled"):
        notif_cfg = pdr_cfg["notifications"]
        notif_body: dict[str, Any] = {"enabled": True, "type": notif_cfg.get("type")}
        if "kafka" in notif_cfg:
            k = notif_cfg["kafka"]
            notif_body["kafka"] = {
                "schema": k.get("schema"),
                "serverURL": k.get("server_url"),
                "connectionType": k.get("connection_type"),
                # Mirrors the COS-side notification topic: one topic per
                # bucket, named after the bucket, unless overridden.
                "topic": k.get("topic", bucket_name),
            }
        if "sqs" in notif_cfg:
            s = notif_cfg["sqs"]
            notif_body["sqs"] = {
                "serverURL": s.get("server_url"),
                "queue": s.get("queue"),
                "accessKey": s.get("access_key"),
                "secretKey": s.get("secret_key"),
            }
        task_body["notifications"] = notif_body

    task, action = client.ensure_task(task_body)
    detail = f"{action} replication task alias='{bucket_name}'"
    if action == "unchanged":
        report.add("pdr.task", changed=False, skipped=True, detail=detail)
    else:
        report.add("pdr.task", changed=True, detail=detail)

    if action == "created" and pdr_cfg.get("auto_start_job") and task is not None and task.get("id") is not None:
        client.start_job(task["id"])
        report.add("pdr.job", changed=True, detail=f"started job for task id={task['id']}")


def sync_bucket(config: dict, bucket_name: str, tenant: str, dry_run: bool = False) -> SyncReport:
    """``tenant`` names the PAG partition to use/create and is always
    required explicitly: several real tenant codes share a common prefix
    (e.g. "ME", "ME-SR", "ME-SRE", "ME-SRE2"), so guessing it from the
    bucket name risks silently picking the wrong tenant.
    """
    report = SyncReport(bucket_name=bucket_name, dry_run=dry_run)

    for section in ("cos", "pag", "pdr"):
        if section not in config:
            raise ConfigError(f"Missing top-level config section '{section}'")

    _sync_cos_bucket(config["cos"], bucket_name, dry_run, report)
    _sync_pag_repository(config["pag"], bucket_name, tenant, dry_run, report)
    _sync_pdr_task(config["pdr"], bucket_name, dry_run, report)

    return report
