import subprocess
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from task_agent import app_token


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem(key: rsa.RSAPrivateKey) -> str:
    form = serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8
    return key.private_bytes(*form, serialization.NoEncryption()).decode()


def test_the_app_token_is_signed_by_the_key_and_lasts_under_ten_minutes(
    key: rsa.RSAPrivateKey,
) -> None:
    token = app_token.App("client-id", pem(key)).jwt()
    claims = jwt.decode(token, key.public_key(), algorithms=["RS256"])
    assert claims["iss"] == "client-id"
    assert claims["exp"] - claims["iat"] == 600


def test_a_repo_token_names_one_repo_and_only_the_permissions_asked_for(
    key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, Any]] = []

    def call(method: str, path: str, token: str, body: Any = None) -> Any:
        calls.append((method, path, body))
        return {"id": 7, "token": "ghs_x"}

    monkeypatch.setattr(app_token, "call", call)
    api = app_token.App("client-id", pem(key)).token("o/repo", issues="write")
    assert (api.repo, api.token) == ("o/repo", "ghs_x")
    assert calls == [
        ("GET", "/repos/o/repo/installation", None),
        (
            "POST",
            "/app/installations/7/access_tokens",
            {"repositories": ["repo"], "permissions": {"issues": "write"}},
        ),
    ]


def test_api_calls_are_scoped_to_the_repo_and_graphql_errors_are_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str, Any]] = []

    def call(method: str, path: str, token: str, body: Any = None) -> Any:
        seen.append((method, path, body))
        return {"errors": [{"message": "no"}]} if path == "/graphql" else {"ok": True}

    monkeypatch.setattr(app_token, "call", call)
    api = app_token.Api("o/repo", "t")
    assert api("GET", "/issues/1") == {"ok": True}
    assert seen[0][:2] == ("GET", "/repos/o/repo/issues/1")
    with pytest.raises(RuntimeError, match="GraphQL"):
        api.graphql("query", number=1)
    assert seen[1][2]["variables"] == {"owner": "o", "name": "repo", "number": 1}


def test_a_key_that_cannot_be_read_stops_with_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = subprocess.CompletedProcess(["op"], 1, stdout="", stderr='"k" isn\'t a document')
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: failed)
    with pytest.raises(SystemExit, match="op could not read 'k' from 'v'.*\n.*make doctor"):
        app_token.read_key("k", "v")
