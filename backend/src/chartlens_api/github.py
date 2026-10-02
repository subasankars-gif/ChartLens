"""Start the production refresh workflow as a GitHub App (ADR-0018).

The API never runs the pipeline. It asks GitHub to run ``production-refresh.yml`` with
the ChartLens ``run_id`` as input, and returns.

Credentials:

- The App's private key comes from Secret Manager, held as a ``SecretStr``.
- It signs a JWT that lives 10 minutes. That JWT is exchanged for an installation token
  limited to this one repository and to *Actions: write*, which lives 1 hour and is
  cached in memory.
- Neither token is logged, put in an exception, or returned by any route.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

log = logging.getLogger("chartlens.api.github")

API = "https://api.github.com"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "chartlens-api",
}


class DispatchFailed(RuntimeError):
    """GitHub did not accept the dispatch. The message is safe to record and show."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class WorkflowDispatcher(Protocol):
    def dispatch(self, run_id: str) -> None:
        """Ask GitHub to start the refresh for ``run_id``; raise :class:`DispatchFailed`."""
        ...


def _app_jwt(app_id: str, private_key: str, now: float) -> str:
    from google.auth import crypt, jwt

    signer = crypt.RSASigner.from_string(private_key)
    # iat 60 s in the past tolerates clock drift; GitHub caps exp at 10 minutes.
    payload = {"iat": int(now) - 60, "exp": int(now) + 540, "iss": app_id}
    token: bytes = jwt.encode(signer, payload)
    return token.decode()


class GitHubAppDispatcher:
    def __init__(
        self,
        *,
        app_id: str,
        private_key: str,
        repository: str,
        workflow: str,
        ref: str,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if repository.count("/") != 1:
            raise ValueError("github_repository must be owner/repo")
        self._app_id = app_id
        self._key = private_key
        self.repository = repository
        self.workflow = workflow
        self.ref = ref
        self._client = client or httpx.Client(base_url=API, headers=HEADERS, timeout=15)
        self._clock = clock
        self._lock = threading.Lock()
        self._installation: int | None = None
        self._token: tuple[str, float] | None = None

    def _call(
        self, method: str, path: str, bearer: str, body: dict[str, Any] | None = None
    ) -> httpx.Response:
        try:
            r = self._client.request(
                method,
                path,
                json=body,
                headers={**HEADERS, "Authorization": f"Bearer {bearer}"},
            )
        except httpx.HTTPError as exc:
            raise DispatchFailed(f"GitHub could not be reached ({type(exc).__name__})") from None
        if r.status_code >= 300:
            try:
                message = str(r.json().get("message", ""))[:200]
            except ValueError:
                message = ""
            log.warning("GitHub %s %s answered %s: %s", method, path, r.status_code, message)
            raise DispatchFailed(f"GitHub answered {r.status_code}", r.status_code)
        return r

    def _installation_token(self) -> str:
        now = self._clock()
        with self._lock:
            if self._token and self._token[1] - 300 > now:
                return self._token[0]
            app = _app_jwt(self._app_id, self._key, now)
            if self._installation is None:
                r = self._call("GET", f"/repos/{self.repository}/installation", app)
                self._installation = int(r.json()["id"])
            r = self._call(
                "POST",
                f"/app/installations/{self._installation}/access_tokens",
                app,
                {
                    "repositories": [self.repository.split("/", 1)[1]],
                    "permissions": {"actions": "write"},
                },
            )
            token = str(r.json()["token"])
            self._token = (token, now + 3600)
            return token

    def dispatch(self, run_id: str) -> None:
        path = f"/repos/{self.repository}/actions/workflows/{self.workflow}/dispatches"
        body = {"ref": self.ref, "inputs": {"run_id": run_id}}
        try:
            self._call("POST", path, self._installation_token(), body)
        except DispatchFailed as exc:
            if exc.status != 401:
                raise
            with self._lock:  # a revoked or expired token: mint a fresh one, once
                self._token = None
            self._call("POST", path, self._installation_token(), body)
        log.info("refresh workflow dispatched for %s", run_id)
