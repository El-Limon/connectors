"""A small GitHub client the maintenance issues share. No #149 command calls it yet."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__, output
from .exit_codes import TrackerError

DEFAULT_API_URL = "https://api.github.com"
_LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


def resolve_token(explicit: str | None = None) -> str:
    """``--token`` → ``GH_TOKEN`` → ``gh auth token``. Never a CI-only variable."""
    if explicit:
        return explicit
    env_token = os.environ.get("GH_TOKEN")
    if env_token:
        return env_token
    if shutil.which("gh"):
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    raise TrackerError("no GitHub token: set GH_TOKEN or run `gh auth login`")


def resolve_repo(explicit: str | None, repo_root: Path) -> str:
    """``--repo`` → ``TAKARO_MAINT_REPO`` → the ``origin`` remote. Never ``GITHUB_REPOSITORY``."""
    if explicit:
        return explicit
    env_repo = os.environ.get("TAKARO_MAINT_REPO")
    if env_repo:
        return env_repo
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    url = result.stdout.strip().removesuffix(".git")
    if not url:
        raise TrackerError("no repository: pass --repo or set TAKARO_MAINT_REPO")
    if ":" in url and "//" not in url:
        return url.split(":", 1)[1]
    return "/".join(url.rstrip("/").split("/")[-2:])


class GitHub:
    """Only the endpoints the maintenance commands actually use."""

    def __init__(self, repo: str, token: str, api_url: str | None = None) -> None:
        self.repo = repo
        self.token = token
        self.api_url = (api_url or os.environ.get("TAKARO_MAINT_GITHUB_API_URL") or DEFAULT_API_URL).rstrip("/")

    # -- transport ------------------------------------------------------------
    def _request(self, method: str, path: str, body: Any = None, *, raw: bool = False) -> Any:
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", f"takaro-connectors-maint/{__version__}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                payload = response.read()
                link = response.headers.get("Link", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise TrackerError(f"{method} {url} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise TrackerError(f"{method} {url} is unreachable: {exc.reason}") from exc
        if raw:
            return payload, link
        if not payload:
            return None, link
        try:
            return json.loads(payload), link
        except json.JSONDecodeError:
            return payload, link

    def get(self, path: str) -> Any:
        return self._request("GET", path)[0]

    def post(self, path: str, body: Any) -> Any:
        return self._request("POST", path, body)[0]

    def patch(self, path: str, body: Any) -> Any:
        return self._request("PATCH", path, body)[0]

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)[0]

    def paginate(self, path: str) -> list[Any]:
        """Follow ``Link: rel="next"`` until the last page."""
        items: list[Any] = []
        next_path: str | None = path
        while next_path:
            payload, link = self._request("GET", next_path)
            if isinstance(payload, list):
                items.extend(payload)
            else:
                items.append(payload)
            match = _LINK_NEXT.search(link or "")
            next_path = match.group(1) if match else None
        return items

    # -- releases -------------------------------------------------------------
    def release_by_tag(self, tag: str) -> dict[str, Any]:
        return self.get(f"/repos/{self.repo}/releases/tags/{urllib.parse.quote(tag)}")  # type: ignore[no-any-return]

    def assets(self, release_id: int) -> list[dict[str, Any]]:
        return self.paginate(f"/repos/{self.repo}/releases/{release_id}/assets?per_page=100")

    def upload_asset(self, release_id: int, file: Path, *, content_type: str = "application/octet-stream") -> Any:
        url = f"https://uploads.github.com/repos/{self.repo}/releases/{release_id}/assets?name={file.name}"
        request = urllib.request.Request(url, data=file.read_bytes(), method="POST")
        # Unredirected, so a redirect off the API host never carries the bearer token.
        request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        request.add_header("Content-Type", content_type)
        request.add_header("User-Agent", f"takaro-connectors-maint/{__version__}")
        try:
            with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            raise TrackerError(f"uploading {file.name} -> HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise TrackerError(f"uploading {file.name} failed: {exc.reason}") from exc

    def download_asset(self, asset_id: int, dest: Path) -> Path:
        url = f"{self.api_url}/repos/{self.repo}/releases/assets/{asset_id}"
        request = urllib.request.Request(url, method="GET")
        # An asset download is a 302 to a signed CDN URL; it must never see the token.
        request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", "application/octet-stream")
        request.add_header("User-Agent", f"takaro-connectors-maint/{__version__}")
        try:
            with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(response.read())
        except urllib.error.HTTPError as exc:
            raise TrackerError(f"downloading asset {asset_id} -> HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise TrackerError(f"downloading asset {asset_id} failed: {exc.reason}") from exc
        return dest

    def delete_asset(self, asset_id: int) -> None:
        self.delete(f"/repos/{self.repo}/releases/assets/{asset_id}")

    # -- issues and pulls -----------------------------------------------------
    def issues_search(self, query: str) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(f"repo:{self.repo} {query}")
        payload = self.get(f"/search/issues?q={encoded}&per_page=100")
        return list(payload.get("items", []))

    def issues_list(self, state: str = "all", labels: str | None = None) -> list[dict[str, Any]]:
        path = f"/repos/{self.repo}/issues?state={state}&per_page=100"
        if labels:
            path += f"&labels={urllib.parse.quote(labels)}"
        return [item for item in self.paginate(path) if "pull_request" not in item]

    def issue_get(self, number: int) -> dict[str, Any]:
        return self.get(f"/repos/{self.repo}/issues/{number}")  # type: ignore[no-any-return]

    def issue_create(self, title: str, body: str, labels: list[str] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        return self.post(f"/repos/{self.repo}/issues", payload)  # type: ignore[no-any-return]

    def issue_update(self, number: int, **fields: Any) -> dict[str, Any]:
        return self.patch(f"/repos/{self.repo}/issues/{number}", fields)  # type: ignore[no-any-return]

    def issue_close(self, number: int, reason: str = "completed") -> dict[str, Any]:
        return self.issue_update(number, state="closed", state_reason=reason)

    def pulls_search(self, query: str) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(f"repo:{self.repo} is:pr {query}")
        payload = self.get(f"/search/issues?q={encoded}&per_page=100")
        return list(payload.get("items", []))

    def contents(self, path: str, ref: str | None = None) -> dict[str, Any]:
        suffix = f"?ref={urllib.parse.quote(ref)}" if ref else ""
        return self.get(f"/repos/{self.repo}/contents/{urllib.parse.quote(path)}{suffix}")  # type: ignore[no-any-return]


def client(repo: str | None, token: str | None, api_url: str | None, repo_root: Path) -> GitHub:
    resolved_repo = resolve_repo(repo, repo_root)
    output.debug(f"github: acting on {resolved_repo}")
    return GitHub(resolved_repo, resolve_token(token), api_url)
