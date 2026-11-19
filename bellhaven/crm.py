"""Thin CRM API client.

Safety properties:
- The token only ever goes into the Authorization header. It is never logged or stored.
- Reads (GET) retry on transient errors. Writes (POST/PATCH) are NEVER retried here: if a
  write times out we do not know whether it landed, so we raise UncertainWrite and let the
  applier reconcile against live CRM data before anything is sent again.
- Every write is reported to an audit callback (method, path, body, status), without headers.
"""
from __future__ import annotations

import time
from typing import Callable, List, Optional

import requests


class CRMError(RuntimeError):
    """The API rejected the request (4xx). Nothing was changed."""


class UncertainWrite(CRMError):
    """A write may or may not have landed (timeout, connection drop, 5xx)."""


class CRMClient:
    def __init__(self, api_base: str, token: str, audit: Optional[Callable[..., None]] = None,
                 session=None, timeout: int = 20):
        self.api_base = api_base.rstrip("/")
        self.audit = audit
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})

    # ---- reads -------------------------------------------------------------
    def _get(self, path: str, params: Optional[dict] = None):
        last_error = None
        for attempt in range(3):
            try:
                response = self.session.get(self.api_base + path, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(1 + attempt)
                continue
            if response.status_code >= 500:
                last_error = CRMError(f"GET {path} -> {response.status_code}")
                time.sleep(1 + attempt)
                continue
            if response.status_code == 404:
                return None
            if response.status_code >= 400:
                raise CRMError(f"GET {path} -> {response.status_code}: {response.text[:200]}")
            return response.json()
        raise CRMError(f"GET {path} failed after retries: {type(last_error).__name__}")

    def _list(self, path: str, **filters) -> List[dict]:
        params = {k: v for k, v in filters.items() if v}
        rows, page = [], 1
        while True:
            body = self._get(path, {**params, "page": page, "page_size": 100}) or {}
            data = body.get("data", [])
            rows.extend(data)
            if not data or len(rows) >= body.get("total", 0):
                return rows
            page += 1

    def me(self) -> dict:
        return self._get("/me") or {}

    def list_accounts(self, **filters) -> List[dict]:
        return self._list("/accounts", **filters)

    def list_contacts(self, **filters) -> List[dict]:
        return self._list("/contacts", **filters)

    def account_total(self) -> int:
        """One authenticated GET (a single row) that returns how many accounts the CRM holds."""
        body = self._get("/accounts", {"page": 1, "page_size": 1})
        if not body or "total" not in body:
            raise CRMError("Account list did not include a total")
        return int(body["total"])

    def get_account(self, account_id: str) -> Optional[dict]:
        return self._get(f"/accounts/{account_id}")

    # ---- writes ------------------------------------------------------------
    def _write(self, method: str, path: str, body: dict) -> dict:
        try:
            response = self.session.request(method, self.api_base + path, json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            self._audit(method, path, body, None, f"no response: {type(exc).__name__}")
            raise UncertainWrite(f"{method} {path}: no response ({type(exc).__name__}); outcome unknown")
        self._audit(method, path, body, response.status_code, response.text[:1000])
        if response.status_code >= 500:
            raise UncertainWrite(f"{method} {path} -> {response.status_code}; outcome unknown")
        if response.status_code >= 400:
            raise CRMError(f"{method} {path} -> {response.status_code}: {response.text[:300]}")
        payload = response.json()
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        return payload

    def _audit(self, method, path, body, status, response_text):
        if self.audit:
            self.audit(method=method, path=path, body=body, status=status, response=response_text)

    def create_account(self, body: dict) -> dict:
        return self._write("POST", "/accounts", body)

    def update_account(self, account_id: str, body: dict) -> dict:
        return self._write("PATCH", f"/accounts/{account_id}", body)
