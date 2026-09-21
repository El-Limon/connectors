"""The release endpoints, including the two that are not JSON: asset upload and download.

Everything goes through the shared :class:`~takaro_maint.github.GitHub` transport except the
two byte-moving calls, which need a different base URL, a different ``Accept`` and a longer
timeout than a JSON request does. Keeping them here means no command reaches for ``urllib``
itself and every HTTP failure surfaces as the same exit code.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .. import __version__
from ..exit_codes import TrackerError
from ..github import GitHub

UPLOAD_TIMEOUT_SECONDS = 300


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


class ReleaseClient:
    """Releases, their assets and their tags, for one repository."""

    def __init__(self, github: GitHub) -> None:
        self.github = github
        self.repo = github.repo

    # -- helpers --------------------------------------------------------------
    def _optional(self, path: str) -> Any:
        """``None`` for a 404; any other HTTP failure is still a failure."""
        try:
            return self.github.get(path)
        except TrackerError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.github.token}",
            "User-Agent": f"takaro-connectors-maint/{__version__}",
        }

    # -- releases -------------------------------------------------------------
    def list_releases(self) -> list[dict[str, Any]]:
        """Every release, drafts included. Drafts are only visible to a token with push access."""
        return [item for item in self.github.paginate(f"/repos/{self.repo}/releases?per_page=100") if item]

    def find_release(self, tag: str) -> dict[str, Any] | None:
        """The release for ``tag``, published or draft.

        ``GET /releases/tags/{tag}`` only ever answers for a published release, and a
        release-please release with ``draft: true`` is exactly the case this has to find, so a
        miss falls back to scanning the listing rather than concluding there is no release.
        """
        published = self._optional(f"/repos/{self.repo}/releases/tags/{_quote(tag)}")
        if published:
            return dict(published)
        for release in self.list_releases():
            if release.get("tag_name") == tag:
                return dict(release)
        return None

    def create_draft(
        self,
        *,
        tag: str,
        target_commitish: str,
        name: str,
        body: str,
        prerelease: bool,
    ) -> dict[str, Any]:
        payload = {
            "tag_name": tag,
            "target_commitish": target_commitish,
            "name": name,
            "body": body,
            "draft": True,
            "prerelease": prerelease,
        }
        return dict(self.github.post(f"/repos/{self.repo}/releases", payload))

    def update(self, release_id: int, **fields: Any) -> dict[str, Any]:
        return dict(self.github.patch(f"/repos/{self.repo}/releases/{release_id}", fields))

    def delete_release(self, release_id: int) -> None:
        self.github.delete(f"/repos/{self.repo}/releases/{release_id}")

    def assets(self, release_id: int) -> list[dict[str, Any]]:
        return [
            item
            for item in self.github.paginate(f"/repos/{self.repo}/releases/{release_id}/assets?per_page=100")
            if item
        ]

    # -- tags -----------------------------------------------------------------
    def tag_commit(self, tag: str) -> str | None:
        """The commit ``refs/tags/<tag>`` resolves to, dereferencing an annotated tag."""
        ref = self._optional(f"/repos/{self.repo}/git/ref/tags/{_quote(tag)}")
        if not ref:
            return None
        obj = ref.get("object") or {}
        if obj.get("type") == "tag":
            annotated = self._optional(f"/repos/{self.repo}/git/tags/{obj['sha']}")
            if annotated:
                return str((annotated.get("object") or {}).get("sha") or "")
        return str(obj.get("sha") or "")

    def delete_tag(self, tag: str) -> None:
        self.github.delete(f"/repos/{self.repo}/git/refs/tags/{_quote(tag)}")

    # -- bytes ----------------------------------------------------------------
    def upload(self, upload_url: str, file: Path) -> dict[str, Any]:
        """POST one file to a release's ``upload_url`` template."""
        base = upload_url.split("{", 1)[0]
        url = f"{base}?name={_quote(file.name)}"
        request = urllib.request.Request(url, data=file.read_bytes(), method="POST")
        for key, value in self._headers().items():
            request.add_header(key, value)
        request.add_header("Content-Type", "application/octet-stream")
        try:
            with urllib.request.urlopen(request, timeout=UPLOAD_TIMEOUT_SECONDS) as response:  # noqa: S310
                return dict(json.loads(response.read() or b"{}"))
        except urllib.error.HTTPError as exc:
            if exc.code == 422:
                raise TrackerError(
                    f"uploading {file.name}: the release already has an asset with that name (race?)",
                    asset=file.name,
                ) from exc
            raise TrackerError(f"uploading {file.name} -> HTTP {exc.code}", asset=file.name) from exc
        except urllib.error.URLError as exc:
            raise TrackerError(f"uploading {file.name} failed: {exc.reason}", asset=file.name) from exc

    def download(self, asset: dict[str, Any]) -> bytes:
        """The asset's bytes, through the API so a draft release's assets are reachable too."""
        url = f"{self.github.api_url}/repos/{self.repo}/releases/assets/{asset['id']}"
        request = urllib.request.Request(url, method="GET")
        for key, value in self._headers().items():
            request.add_header(key, value)
        request.add_header("Accept", "application/octet-stream")
        try:
            with urllib.request.urlopen(request, timeout=UPLOAD_TIMEOUT_SECONDS) as response:  # noqa: S310
                return bytes(response.read())
        except urllib.error.HTTPError as exc:
            raise TrackerError(f"downloading {asset.get('name')} -> HTTP {exc.code}", asset=asset.get("name")) from exc
        except urllib.error.URLError as exc:
            raise TrackerError(
                f"downloading {asset.get('name')} failed: {exc.reason}", asset=asset.get("name")
            ) from exc
