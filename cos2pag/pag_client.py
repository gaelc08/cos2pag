"""Client for the PoINT Archival Gateway (PAG) Administration API.

A "bucket" on PAG is an Object Repository, created under a Partition:
``POST /api/partitions/{partitionUuid}/repositories``.
"""
from __future__ import annotations

from typing import Any

import requests

from .http_client import ApiError, request_json


class PagClient:
    def __init__(self, base_url: str, session: requests.Session, timeout: int = 30, dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.timeout = timeout
        self.dry_run = dry_run

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def list_partitions(self) -> list[dict[str, Any]]:
        partitions: list[dict[str, Any]] = []
        prev_index = 0
        while True:
            page = request_json(
                self.session,
                "GET",
                self._url("/api/partitions"),
                timeout=self.timeout,
                params={"prevIndex": prev_index, "maxCount": 100},
            ) or []
            if not page:
                break
            partitions.extend(page)
            if len(page) < 100:
                break
            prev_index = page[-1]["index"]
        return partitions

    def find_partition(self, name_or_uuid: str) -> dict[str, Any]:
        for partition in self.list_partitions():
            if partition.get("uuid") == name_or_uuid or partition.get("name") == name_or_uuid:
                return partition
        raise LookupError(f"PAG partition '{name_or_uuid}' not found")

    def list_repositories(self, partition_uuid: str) -> list[dict[str, Any]]:
        repositories: list[dict[str, Any]] = []
        prev_index = 0
        while True:
            page = request_json(
                self.session,
                "GET",
                self._url(f"/api/partitions/{partition_uuid}/repositories"),
                timeout=self.timeout,
                params={"prevIndex": prev_index, "maxCount": 100},
            ) or []
            if not page:
                break
            repositories.extend(page)
            if len(page) < 100:
                break
            prev_index = page[-1]["index"]
        return repositories

    def find_repository(self, partition_uuid: str, name: str) -> dict[str, Any] | None:
        for repo in self.list_repositories(partition_uuid):
            if repo.get("name") == name:
                return repo
        return None

    def create_repository(self, partition_uuid: str, body: dict[str, Any]) -> dict[str, Any] | None:
        return request_json(
            self.session,
            "POST",
            self._url(f"/api/partitions/{partition_uuid}/repositories"),
            timeout=self.timeout,
            json=body,
            dry_run=self.dry_run,
        )

    def update_repository(self, partition_uuid: str, repository_uuid: str, body: dict[str, Any]) -> dict[str, Any] | None:
        return request_json(
            self.session,
            "PATCH",
            self._url(f"/api/partitions/{partition_uuid}/repositories/{repository_uuid}"),
            timeout=self.timeout,
            json=body,
            dry_run=self.dry_run,
        )

    def ensure_repository(
        self,
        partition_uuid: str,
        name: str,
        owner: dict[str, Any],
        hidden: bool | None = None,
        write_protected: bool | None = None,
        sosapi_enabled: bool | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """Create the repository if missing, or patch it only if something
        actually differs from an existing one.

        Returns ``(repository, action)`` where action is one of
        "created", "updated", "unchanged". Some PAG licenses don't cover
        `PATCH` on an existing repository (`ModifyObjectRepository`), so
        this avoids calling it at all when there's nothing to change.
        """
        existing = self.find_repository(partition_uuid, name)
        body: dict[str, Any] = {"name": name, "owner": owner}
        if hidden is not None:
            body["hidden"] = hidden
        if write_protected is not None:
            body["writeProtected"] = write_protected
        if sosapi_enabled is not None:
            body["sosapiEnabled"] = sosapi_enabled

        if existing is None:
            return self.create_repository(partition_uuid, body), "created"

        existing_owner = existing.get("owner") or {}
        needs_update = (
            existing_owner.get("uuid") != owner.get("uuid")
            or existing_owner.get("type") != owner.get("type")
            or (hidden is not None and existing.get("hidden") != hidden)
            or (write_protected is not None and existing.get("writeProtected") != write_protected)
            or (sosapi_enabled is not None and existing.get("sosapiEnabled") != sosapi_enabled)
        )
        if not needs_update:
            return existing, "unchanged"
        return self.update_repository(partition_uuid, existing["uuid"], body), "updated"


__all__ = ["PagClient", "ApiError"]
