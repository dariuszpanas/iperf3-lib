"""Check built documentation and rendered package-description links without network access."""

from __future__ import annotations

import argparse
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from readme_renderer.markdown import render

SITE = "https://dariuszpanas.github.io/iperf3-lib/"


class DocumentLinks(HTMLParser):
    """Collect linked pages, HTML asset references, and named anchors."""

    def __init__(self) -> None:
        """Create an empty document index."""
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.anchors: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Index standard HTML link attributes and anchor declarations."""
        attributes = dict(attrs)
        if anchor := attributes.get("id"):
            self.anchors.add(anchor)
        if tag == "a" and (anchor := attributes.get("name")):
            self.anchors.add(anchor)
        for attribute in ("href", "src", "poster"):
            if (link := attributes.get(attribute)) is not None:
                self.links.append(link)


def _parse(html: str) -> DocumentLinks:
    parsed = DocumentLinks()
    parsed.feed(html)
    parsed.close()
    return parsed


def _readme_links(readme: Path) -> DocumentLinks:
    html = render(readme.read_text(encoding="utf-8"))
    if not html:
        raise ValueError("README package description did not render")
    parsed = _parse(html)
    for link in parsed.links:
        if link.startswith("#"):
            if unquote(link[1:]) not in parsed.anchors:
                raise ValueError(f"README: missing fragment target: {link}")
            continue
        url = urlsplit(link)
        if url.scheme != "https" or not url.netloc:
            raise ValueError(f"README: package description requires absolute HTTPS links: {link}")
    return parsed


def _local_target(site: Path, source_url: str, link: str, site_url: str) -> Path | None:
    original = urlsplit(link)
    resolved = urlsplit(urljoin(source_url, link))
    base = urlsplit(site_url)
    if (resolved.scheme, resolved.netloc) != (base.scheme, base.netloc):
        return None
    prefix = unquote(base.path)
    path = unquote(resolved.path)
    if not path.startswith(prefix):
        if original.scheme or original.netloc:
            return None  # An absolute link to another project on the same host.
        raise ValueError(f"link escapes the site: {link}")
    local = path.removeprefix(prefix)
    if "\\" in local:
        raise ValueError(f"link contains a backslash instead of a URL separator: {link}")
    target = (site / local).resolve()
    if not target.is_relative_to(site):
        raise ValueError(f"link escapes the site: {link}")
    if target.is_dir():
        target /= "index.html"
    return target


def check_site(site: Path, readme: Path, *, site_url: str = SITE) -> int:
    """Reject missing local pages, assets, or HTML fragments and nonportable README links.

    External URLs are deliberately not fetched. The README is rendered using
    PyPI's Markdown renderer so package links must work outside the repository.
    """
    base = urlsplit(site_url)
    if base.scheme != "https" or not base.netloc or not base.path.endswith("/"):
        raise ValueError("site URL must be absolute HTTPS with a trailing slash")
    if base.query or base.fragment:
        raise ValueError("site URL must not include a query or fragment")
    site = site.resolve()
    pages = {
        path.resolve(): _parse(path.read_text(encoding="utf-8")) for path in site.rglob("*.html")
    }
    if not pages:
        raise ValueError("build the documentation before checking its links")
    sources = [(path.relative_to(site).as_posix(), page) for path, page in pages.items()]
    sources.append(("README", _readme_links(readme)))
    checked = 0
    for relative, page in sources:
        source_url = urljoin(site_url, relative)
        for link in page.links:
            if relative == "README" and link.startswith("#"):
                continue  # README anchors were checked against its own rendered HTML.
            try:
                target = _local_target(site, source_url, link, site_url)
                if target is None:
                    continue
                if not target.is_file():
                    raise ValueError(f"missing link target: {link}")
                fragment = unquote(urlsplit(link).fragment)
                if fragment and target in pages and fragment not in pages[target].anchors:
                    raise ValueError(f"missing fragment target: {link}")
            except ValueError as error:
                raise ValueError(f"{relative}: {error}") from error
            checked += 1
    return checked


def main() -> int:
    """Check the built site and README, optionally using explicit paths."""
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, default=root / "site")
    parser.add_argument("--readme", type=Path, default=root / "README.md")
    parser.add_argument("--site-url", default=SITE)
    args = parser.parse_args()
    try:
        count = check_site(args.site, args.readme, site_url=args.site_url)
    except (OSError, ValueError) as error:
        print(f"documentation link validation failed: {error}", file=sys.stderr)
        return 1
    print(f"Checked {count} local documentation and README links.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
