"""Make a package machine-neutral: drop runtime state, rewrite build paths, scan for leaks."""

import gzip
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
lazy from collections.abc import Iterable

from dbeaver_portable import distribution

log = logging.getLogger(__name__)

STATE_AREAS = ("configuration", "p2")
BUNDLES_INFO = "configuration/org.eclipse.equinox.simpleconfigurator/bundles.info"
# A bundles.info line is "name,version,location,start level,autostart"; p2 installs extensions
# into a bundle pool (with its own features/ and index files) inside the configuration area.
POOLED_BUNDLE = re.compile(r"[^#,][^,]*,[^,]*,(configuration/.+?)/plugins/")
BUNDLE_LOCATION = re.compile(r"[^#,][^,]*,[^,]*,([^,]+),")
BINARY_SUFFIXES = frozenset(
    {".bmp", ".dll", ".exe", ".gif", ".ico", ".jar", ".jpg", ".png", ".zip"}
)
MIN_COMPUTER_NAME = 6  # shorter names would match random bytes


def path_variants(path: Path) -> list[str]:
    """Return the spellings of a path that Windows and Java tools write, longest first."""
    native = str(path)
    slashed = native.replace("\\", "/")
    variants = {
        native,
        slashed,
        native.replace("\\", "\\\\"),  # JSON and Java string escapes
        native.replace("\\", "\\\\").replace(":", "\\:"),  # .properties escapes
        urllib.parse.quote(slashed, safe="/:"),  # file: URLs
    }
    return sorted(variants, key=len, reverse=True)


def rewrite_paths(text: str, old: Path, new: str) -> str:
    """Replace every spelling of old in text with new, ignoring case as Windows paths do."""
    pattern = "|".join(map(re.escape, path_variants(old)))
    return re.sub(pattern, lambda _: new, text, flags=re.IGNORECASE)


def _rewrite_file(path: Path, old: Path, new: str) -> None:
    raw = path.read_bytes()
    compressed = path.suffix == ".gz"
    data = gzip.decompress(raw) if compressed else raw
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return
    rewritten = rewrite_paths(text, old, new)
    if rewritten != text:
        encoded = rewritten.encode("utf-8")
        path.write_bytes(gzip.compress(encoded, mtime=0) if compressed else encoded)


def bundle_pools(app_dir: Path) -> set[str]:
    """Return the app-relative folders of the p2 bundle pools that bundles.info loads from."""
    path = app_dir / BUNDLES_INFO
    lines = path.read_text("utf-8").splitlines() if path.is_file() else []
    return {match.group(1) for line in lines if (match := POOLED_BUNDLE.match(line))}


def missing_bundles(app_dir: Path) -> list[str]:
    """Return the bundles.info locations absent from the app: DBeaver would not load them."""
    lines = (app_dir / BUNDLES_INFO).read_text("utf-8").splitlines()
    locations = [match.group(1) for line in lines if (match := BUNDLE_LOCATION.match(line))]
    return [location for location in locations if not (app_dir / location).exists()]


def scrub_app(app_dir: Path, pristine: frozenset[str], original_location: str) -> None:
    """
    Reduce an installation's configuration/ and p2/ to what DBeaver ships plus the p2 install.

    Kept: the shipped files, the newest profile snapshot and the bundle pools holding installed
    extensions. DBeaver regenerates the rest at runtime (OSGi caches, logs, recent workspaces,
    p2 download caches, intermediate profile snapshots), so it is deleted. The build path left in
    kept files becomes original_location, the path DBeaver's own profile carries: the profile is
    roaming, so p2 relocates it to wherever the package is installed.
    """
    keep = distribution.latest_profile(app_dir)
    pools = tuple(f"{pool}/" for pool in bundle_pools(app_dir))
    for area in STATE_AREAS:
        for path in sorted((app_dir / area).rglob("*"), reverse=True):  # children before parents
            relative = path.relative_to(app_dir).as_posix()
            if path.is_dir():
                if not any(path.iterdir()):
                    path.rmdir()
            elif path != keep and relative not in pristine and not relative.startswith(pools):
                path.unlink()
    for area in STATE_AREAS:
        for path in (app_dir / area).rglob("*"):
            if path.is_file() and path.suffix.lower() not in BINARY_SUFFIXES:
                _rewrite_file(path, app_dir, original_location)


def leak_tokens(*paths: Path) -> list[str]:
    """
    Return strings that must not appear in a package.

    These are the user profile and the given paths in every spelling, plus the computer name.
    """
    tokens = [v for path in (Path.home(), *paths) for v in path_variants(path)]
    computer = os.environ.get("COMPUTERNAME", "")
    if len(computer) >= MIN_COMPUTER_NAME:
        tokens.append(computer)
    return list(dict.fromkeys(tokens))


@dataclass(frozen=True)
class Leak:
    """A machine-specific token found in a file."""

    path: Path
    token: str


def find_leaks(root: Path, tokens: Iterable[str]) -> list[Leak]:
    """Return every occurrence of tokens in the non-binary files under root (case-insensitive)."""
    needles = [
        (token, (token.lower().encode("utf-8"), token.lower().encode("utf-16-le")))
        for token in tokens
        if token
    ]
    leaks = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() in BINARY_SUFFIXES:
            continue
        data = path.read_bytes()
        if path.suffix == ".gz":
            data = gzip.decompress(data)
        data = data.lower()
        leaks += [Leak(path, token) for token, forms in needles if any(f in data for f in forms)]
    return leaks
