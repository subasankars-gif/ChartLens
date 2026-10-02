"""The GitHub App dispatcher (ADR-0018), against a mocked GitHub: a signed short-lived
JWT, a repository- and permission-scoped installation token, the dispatch itself, and
no credential in any error."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import jwt

from chartlens_api.github import API, HEADERS, DispatchFailed, GitHubAppDispatcher

REPO = "subasankars-gif/ChartLens"
NOW = time.time()  # the mock verifies the JWT against the real clock


@pytest.fixture(scope="module")
def keypair() -> tuple[str, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,  # what GitHub hands out (PKCS#1)
        serialization.NoEncryption(),
    ).decode()
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private, public


class FakeGitHub:
    def __init__(self, public: bytes) -> None:
        self.public = public
        self.calls: list[tuple[str, str, dict[str, Any] | None, str]] = []
        self.dispatch_status = 204
        self.tokens = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        bearer = request.headers["Authorization"].removeprefix("Bearer ")
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body, bearer))
        if request.url.path.endswith("/installation"):
            claims = jwt.decode(bearer, certs=self.public, verify=True)
            assert claims["iss"] == "123" and claims["exp"] - claims["iat"] <= 600
            return httpx.Response(200, json={"id": 77})
        if request.url.path == "/app/installations/77/access_tokens":
            jwt.decode(bearer, certs=self.public, verify=True)
            self.tokens += 1
            return httpx.Response(201, json={"token": f"ghs_secret{self.tokens}"})
        if request.url.path.endswith("/dispatches"):
            if self.dispatch_status == 401:  # the first token was revoked
                if bearer == "ghs_secret1":
                    return httpx.Response(401, json={"message": "Bad credentials"})
                return httpx.Response(204)
            return httpx.Response(self.dispatch_status, json={"message": "nope"})
        return httpx.Response(404)


def dispatcher(keypair: tuple[str, bytes], gh: FakeGitHub) -> GitHubAppDispatcher:
    client = httpx.Client(base_url=API, headers=HEADERS, transport=httpx.MockTransport(gh))
    return GitHubAppDispatcher(
        app_id="123",
        private_key=keypair[0],
        repository=REPO,
        workflow="production-refresh.yml",
        ref="main",
        client=client,
        clock=lambda: NOW,
    )


def test_dispatch_uses_a_scoped_installation_token(keypair: tuple[str, bytes]) -> None:
    gh = FakeGitHub(keypair[1])
    d = dispatcher(keypair, gh)
    d.dispatch("run-20261002T150000Z-abc123")
    d.dispatch("run-2")
    paths = [(m, p) for m, p, _, _ in gh.calls]
    assert paths == [
        ("GET", f"/repos/{REPO}/installation"),
        ("POST", "/app/installations/77/access_tokens"),
        ("POST", f"/repos/{REPO}/actions/workflows/production-refresh.yml/dispatches"),
        ("POST", f"/repos/{REPO}/actions/workflows/production-refresh.yml/dispatches"),
    ]  # the installation token is cached
    token_request = gh.calls[1][2]
    assert token_request == {"repositories": ["ChartLens"], "permissions": {"actions": "write"}}
    dispatch_body = gh.calls[2][2]
    assert dispatch_body == {"ref": "main", "inputs": {"run_id": "run-20261002T150000Z-abc123"}}
    assert gh.calls[2][3] == "ghs_secret1"


def test_a_rejected_token_is_replaced_once(keypair: tuple[str, bytes]) -> None:
    gh = FakeGitHub(keypair[1])
    gh.dispatch_status = 401
    d = dispatcher(keypair, gh)
    d.dispatch("run-1")  # 401 with the first token, then succeeds with a fresh one
    assert gh.tokens == 2 and gh.calls[-1][3] == "ghs_secret2"


def test_failures_never_carry_a_credential(keypair: tuple[str, bytes]) -> None:
    gh = FakeGitHub(keypair[1])
    gh.dispatch_status = 422
    d = dispatcher(keypair, gh)
    with pytest.raises(DispatchFailed) as err:
        d.dispatch("run-1")
    assert str(err.value) == "GitHub answered 422"
    assert "ghs_" not in repr(err.value) and "PRIVATE" not in repr(err.value)

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    offline = GitHubAppDispatcher(
        app_id="123",
        private_key=keypair[0],
        repository=REPO,
        workflow="w.yml",
        ref="main",
        client=httpx.Client(base_url=API, transport=httpx.MockTransport(unreachable)),
        clock=lambda: NOW,
    )
    with pytest.raises(DispatchFailed, match="could not be reached"):
        offline.dispatch("run-1")
