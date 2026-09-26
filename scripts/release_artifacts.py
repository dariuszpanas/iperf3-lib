"""Inspect a single wheel/sdist pair and seal its identity for release jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path
from urllib.parse import unquote, urldefrag, urlsplit
from urllib.request import urlopen


def digest(path: Path) -> str:
    """Return the SHA-256 of a complete artifact."""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def distribution_files(directory: Path, version: str) -> list[Path]:
    """Require exactly the expected universal wheel and source archive."""
    expected = {f"iperf3_lib-{version}-py3-none-any.whl", f"iperf3_lib-{version}.tar.gz"}
    actual = {path.name for path in directory.iterdir()}
    marker = directory / ".gitignore"
    if marker.is_file() and marker.read_bytes() in (b"*", b"*\n", b"*\r\n"):
        actual.discard(".gitignore")  # uv build's output-directory marker is not a distribution.
    if actual != expected or not all((directory / name).is_file() for name in expected):
        raise ValueError(f"expected exactly {sorted(expected)}, found {sorted(actual)}")
    return sorted(directory / name for name in expected)


def metadata_description(metadata: str, version: str) -> str:
    """Check package identity and the CFFI-only runtime dependency boundary."""
    parsed = Parser().parsestr(metadata)
    if parsed["Name"] != "iperf3-lib" or parsed["Version"] != version:
        raise ValueError("distribution metadata does not match project identity/version")
    requirements = parsed.get_all("Requires-Dist", [])
    names = [re.split(r"[\s(<>=!~;\[]", value, maxsplit=1)[0].lower() for value in requirements]
    if names != ["cffi"]:
        raise ValueError(f"expected CFFI-only runtime dependencies, found {requirements}")
    if parsed["Requires-Python"] != ">=3.12":
        raise ValueError("distribution Python requirement differs from the supported policy")
    if parsed["Description-Content-Type"] != "text/markdown":
        raise ValueError("distribution description must use Markdown")
    payload = parsed.get_payload()
    if not isinstance(payload, str) or not payload.strip():
        raise ValueError("distribution has no package description")
    return payload


def inspect_distributions(directory: Path, version: str, readme: Path, site: Path) -> None:
    """Inspect archive metadata and render both embedded package descriptions."""
    from scripts.check_docs import check_site

    descriptions: list[str] = []
    for artifact in distribution_files(directory, version):
        if artifact.suffix == ".whl":
            with zipfile.ZipFile(artifact) as archive:
                names = archive.namelist()
                metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
                if len(metadata_names) != 1 or "iperf3_lib/py.typed" not in names:
                    raise ValueError("wheel must contain one metadata record and py.typed")
                descriptions.append(
                    metadata_description(archive.read(metadata_names[0]).decode(), version)
                )
        else:
            with tarfile.open(artifact, "r:gz") as archive:
                prefix = f"iperf3_lib-{version}/"
                for required in (
                    "PKG-INFO",
                    "pyproject.toml",
                    "README.md",
                    "src/iperf3_lib/py.typed",
                ):
                    member = archive.getmember(prefix + required)
                    if not member.isfile():
                        raise ValueError(f"sdist member must be a regular file: {required}")
                metadata = archive.extractfile(prefix + "PKG-INFO")
                project = archive.extractfile(prefix + "pyproject.toml")
                if metadata is None or project is None:
                    raise ValueError("sdist metadata is unreadable")
                descriptions.append(metadata_description(metadata.read().decode(), version))
                if tomllib.loads(project.read().decode())["project"]["version"] != version:
                    raise ValueError("sdist project version differs from its metadata")
    expected_readme = readme.read_text(encoding="utf-8").strip()
    for description in descriptions:
        if description.strip() != expected_readme:
            raise ValueError("embedded distribution description differs from the candidate README")
        with tempfile.TemporaryDirectory() as temporary:
            embedded = Path(temporary) / "README.md"
            embedded.write_text(description, encoding="utf-8")
            check_site(site, embedded)


def write_manifest(directory: Path, path: Path, *, version: str, revision: str) -> str:
    """Seal a validated pair with source identity, filenames, sizes and hashes."""
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("artifact revision must be a full commit SHA")
    manifest = {
        "schema_version": 1,
        "version": version,
        "revision": revision,
        "build_python": sys.version,
        "files": {
            artifact.name: {"sha256": digest(artifact), "size": artifact.stat().st_size}
            for artifact in distribution_files(directory, version)
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return digest(path)


def verify_manifest(
    directory: Path, manifest: Path, *, version: str, revision: str, manifest_sha256: str
) -> None:
    """Reject changed, additional, missing, or differently identified release files."""
    if digest(manifest) != manifest_sha256:
        raise ValueError("release manifest hash mismatch")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if (
        data.get("schema_version") != 1
        or data.get("version") != version
        or data.get("revision") != revision
    ):
        raise ValueError("release manifest identity mismatch")
    files = distribution_files(directory, version)
    if set(data["files"]) != {path.name for path in files}:
        raise ValueError("release manifest file list mismatch")
    for artifact in files:
        recorded = data["files"][artifact.name]
        if recorded != {"sha256": digest(artifact), "size": artifact.stat().st_size}:
            raise ValueError(f"release artifact changed: {artifact.name}")


def check_public_documentation(readme: Path) -> None:
    """Fetch every README documentation URL and validate deployed HTML fragments."""
    from scripts.check_docs import SITE, _parse, _readme_links

    base = urlsplit(SITE)
    links = {SITE, SITE + "changelog.html"}
    for link in _readme_links(readme).links:
        parsed = urlsplit(link)
        if (parsed.scheme, parsed.netloc) == (base.scheme, base.netloc) and parsed.path.startswith(
            base.path
        ):
            links.add(link)
    pages: dict[str, set[str]] = {}
    for link in sorted(links):
        url, fragment = urldefrag(link)
        if url not in pages:
            with urlopen(url, timeout=30) as response:
                if response.status != 200:
                    raise ValueError(f"public documentation is unavailable: {url}")
                content_type = response.headers.get("Content-Type", "")
                is_html = "text/html" in content_type
                if Path(urlsplit(url).path).suffix in ("", ".html", ".htm") and not is_html:
                    raise ValueError(f"public documentation is not HTML: {url}")
                pages[url] = _parse(response.read().decode("utf-8")).anchors if is_html else set()
        if fragment and unquote(fragment) not in pages[url]:
            raise ValueError(f"public documentation is missing a fragment target: {link}")


def main() -> int:
    """Inspect/seal or verify a release bundle from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("inspect", "verify"))
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--manifest", type=Path, default=Path("release-evidence/manifest.json"))
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--site", type=Path, default=Path("site"))
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    parser.add_argument("--check-public-links", action="store_true")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        if args.operation == "verify":
            if not args.manifest_sha256:
                raise ValueError("verification requires the independently retained manifest hash")
            verify_manifest(
                args.dist,
                args.manifest,
                version=args.version,
                revision=args.revision,
                manifest_sha256=args.manifest_sha256,
            )
        else:
            inspect_distributions(args.dist, args.version, args.readme, args.site)
            if args.check_public_links:
                check_public_documentation(args.readme)
            manifest_hash = write_manifest(
                args.dist, args.manifest, version=args.version, revision=args.revision
            )
            if args.github_output:
                with args.github_output.open("a", encoding="utf-8") as output:
                    output.write(f"manifest-sha256={manifest_hash}\n")
        print(f"release artifacts {args.operation} passed for {args.version} at {args.revision}")
    except (OSError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"release artifact validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
