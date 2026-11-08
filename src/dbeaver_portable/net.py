"""HTTPS downloads with retries, checksum verification and a per-user cache folder."""

import hashlib
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from importlib.metadata import version
from pathlib import Path
from typing import BinaryIO
lazy from collections.abc import Callable
lazy from http.client import HTTPResponse

from dbeaver_portable import PortableError

log = logging.getLogger(__name__)

ATTEMPTS = 3
CHUNK_SIZE = 1 << 20
HTTP_NOT_FOUND = 404
HTTP_SERVER_ERROR = 500
PROGRESS_STEP = 5  # percent
TIMEOUT_SECONDS = 60
USER_AGENT = f"dbeaver-portable/{version('dbeaver-portable')} (+https://github.com/ivanlonel/dbeaver-portable)"


class DownloadError(PortableError):
    """A download failed or its content does not match the published checksum."""


def cache_dir() -> Path:
    """Return the per-user folder where immutable downloads are kept between builds."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "dbeaver-portable" / "cache"


def _request(url: str) -> urllib.request.Request:
    if not url.startswith("https://"):
        msg = f"refusing to download over plain HTTP: {url}"
        raise DownloadError(msg)
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310 -- https only


def _with_retries[T](url: str, action: Callable[[HTTPResponse], T]) -> T:
    """Open url and hand the response to action, retrying transient failures with backoff."""
    request = _request(url)
    attempt = 1
    while True:
        try:
            # https only, enforced by _request.
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
                return action(response)
        except urllib.error.HTTPError as error:
            if error.code == HTTP_NOT_FOUND:
                raise FileNotFoundError(url) from error
            if error.code < HTTP_SERVER_ERROR or attempt >= ATTEMPTS:
                raise
        except urllib.error.URLError, TimeoutError, ConnectionError:
            if attempt >= ATTEMPTS:
                raise
        delay = 2**attempt
        log.warning("Retrying %s in %d s (attempt %d of %d failed)", url, delay, attempt, ATTEMPTS)
        time.sleep(delay)
        attempt += 1


def fetch(url: str) -> bytes:
    """Return the body at url; raise FileNotFoundError when the server answers 404."""
    return _with_retries(url, lambda response: response.read())


def file_digest(path: Path, algorithm: str) -> str:
    """Return the hex digest of path (md5/sha1 only ever match vendor-published checksums)."""
    with path.open("rb") as file:
        return hashlib.file_digest(
            file, lambda: hashlib.new(algorithm, usedforsecurity=False)
        ).hexdigest()


def _copy(response: HTTPResponse, out: BinaryIO, label: str, *, show_progress: bool) -> None:
    total = int(response.headers.get("Content-Length") or 0)
    show_progress = show_progress and total > 0 and sys.stderr.isatty()
    done = shown = 0
    while chunk := response.read(CHUNK_SIZE):
        out.write(chunk)
        done += len(chunk)
        if show_progress and (percent := done * 100 // total) >= shown + PROGRESS_STEP:
            shown = percent
            sys.stderr.write(f"\r    {label}: {percent:3d}% of {total / 1e6:.1f} MB")
    if show_progress:
        sys.stderr.write("\n")


def download(
    url: str,
    dest: Path,
    *,
    digest: tuple[str, str] | None = None,
    show_progress: bool = False,
) -> Path:
    """
    Stream url into dest, verifying the (algorithm, hex digest) pair when given.

    The bytes land in a temporary sibling that is renamed only after verification, so an
    interrupted or corrupt download never leaves a file that a later build would trust.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")

    def save(response: HTTPResponse) -> None:
        with partial.open("wb") as out:
            _copy(response, out, dest.name, show_progress=show_progress)

    _with_retries(url, save)
    if digest is not None:
        algorithm, expected = digest
        actual = file_digest(partial, algorithm)
        if actual != expected.strip().lower():
            partial.unlink()
            msg = f"{algorithm} mismatch for {url}: expected {expected}, got {actual}"
            raise DownloadError(msg)
    partial.replace(dest)
    return dest
