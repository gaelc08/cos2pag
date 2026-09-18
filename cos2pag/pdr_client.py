"""Client for the PoINT Data Replicator (PDR) Administration API."""
from __future__ import annotations

from typing import Any

import requests

from .http_client import request_json


class PdrClient:
    def __init__(self, base_url: str, session: requests.Session, timeout: int = 30, dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.timeout = timeout
        self.dry_run = dry_run

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def list_tasks(self) -> list[dict[str, Any]]:
        return request_json(self.session, "GET", self._url("/api/tasks"), timeout=self.timeout) or []

    def find_task_by_alias(self, alias: str) -> dict[str, Any] | None:
        for task in self.list_tasks():
            if task.get("alias") == alias:
                return task
        return None

    def create_task(self, body: dict[str, Any]) -> dict[str, Any] | None:
        return request_json(
            self.session,
            "POST",
            self._url("/api/tasks"),
            timeout=self.timeout,
            json=body,
            dry_run=self.dry_run,
        )

    def ensure_task(self, body: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        """Create the replication task if no task with this alias exists yet.

        Returns ``(task, created)``.
        """
        alias = body.get("alias")
        existing = self.find_task_by_alias(alias) if alias else None
        if existing is not None:
            return existing, False
        return self.create_task(body), True

    def start_job(self, task_id: int, job_type: str = "Copy", filter_path: str | None = None) -> dict[str, Any] | None:
        job_body: dict[str, Any] = {"jobType": job_type}
        if filter_path:
            job_body["filterPath"] = filter_path
        return request_json(
            self.session,
            "POST",
            self._url(f"/api/tasks/{task_id}/jobs"),
            timeout=self.timeout,
            json=job_body,
            dry_run=self.dry_run,
        )
