"""GitHub App tokens. The key is held in memory and this code never writes it to disk."""

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

import jwt

API = "https://api.github.com"


def read_key(title: str, vault: str) -> str:
    """The App's private key, from the file TASK_AGENT_KEY_FILE names or else from 1Password."""
    if path := os.environ.get("TASK_AGENT_KEY_FILE"):
        try:
            return Path(path).read_text()
        except OSError as exc:
            raise SystemExit(f"could not read the key file {path}: {exc}") from None
    cmd = ["op", "document", "get", title, "--vault", vault]
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode:
        why = f"op could not read {title!r} from {vault!r}: {done.stderr.strip()}"
        fix = "Fix: run `make doctor`, or name a file that holds the key in TASK_AGENT_KEY_FILE"
        raise SystemExit(f"{why}\n{fix}")
    return done.stdout


def call(method: str, path: str, token: str, body: Any = None) -> Any:
    """One GitHub API call; returns the parsed JSON, or None for an empty response."""
    request = urllib.request.Request(
        API + path,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


class Api:
    """Calls on one repo with one installation token."""

    def __init__(self, repo: str, token: str) -> None:
        self.repo, self.token = repo, token

    def __call__(self, method: str, path: str, body: Any = None) -> Any:
        return call(method, f"/repos/{self.repo}{path}", self.token, body)

    def graphql(self, query: str, **variables: Any) -> Any:
        """Run a GraphQL query with $owner and $name set to this repo; returns its data."""
        owner, name = self.repo.split("/")
        body = {"query": query, "variables": {"owner": owner, "name": name} | variables}
        answer = call("POST", "/graphql", self.token, body)
        if answer.get("errors"):
            raise RuntimeError(f"GraphQL: {answer['errors']}")
        return answer["data"]


class App:
    """The GitHub App, able to mint short-lived tokens for one repo at a time."""

    def __init__(self, client_id: str, key: str) -> None:
        self.client_id, self._key = client_id, key

    def jwt(self) -> str:
        """A signed token that identifies the App itself for the next nine minutes."""
        now = int(time.time())
        claims = {"iat": now - 60, "exp": now + 540, "iss": self.client_id}
        return jwt.encode(claims, self._key, algorithm="RS256")

    def token(self, repo: str, **permissions: str) -> Api:
        """A token for this one repo with only these permissions; it expires within an hour."""
        app_jwt = self.jwt()
        installation = call("GET", f"/repos/{repo}/installation", app_jwt)
        scope = {"repositories": [repo.split("/")[1]], "permissions": permissions}
        path = f"/app/installations/{installation['id']}/access_tokens"
        return Api(repo, call("POST", path, app_jwt, scope)["token"])
