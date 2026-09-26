"""Exercise offline documentation and PyPI description link validation."""

from pathlib import Path

import pytest

from scripts.check_docs import SITE, DocumentLinks, check_site


@pytest.fixture
def documentation(tmp_path: Path) -> tuple[Path, Path]:
    """Create a minimal generated site and portable Markdown description."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text('<h1 id="start">Start</h1>', encoding="utf-8")
    readme = tmp_path / "README.md"
    readme.write_text(f"# Package\n\n[Docs]({SITE}#start)", encoding="utf-8")
    return site, readme


def test_html_parser_indexes_links_assets_and_anchors() -> None:
    """Include script, stylesheet, image, and video references in the index."""
    parsed = DocumentLinks()
    parsed.feed(
        '<h1 id="title">Title</h1><a name="legacy" href="guide.html">Guide</a>'
        '<link rel="stylesheet" href="style.css"><script src="script.js"></script>'
        '<img src="picture.png"/><video poster="poster.png"></video>'
    )
    assert parsed.anchors == {"title", "legacy"}
    assert parsed.links == ["guide.html", "style.css", "script.js", "picture.png", "poster.png"]


@pytest.mark.parametrize("link", ["guide.html", "missing.css", "guide.html#absent", "#absent"])
def test_missing_documentation_targets_fail(documentation: tuple[Path, Path], link: str) -> None:
    """Reject missing files and fragments in the generated documentation."""
    site, readme = documentation
    (site / "index.html").write_text(f'<h1 id="start">Start</h1><a href="{link}">Link</a>')
    if link.startswith("guide.html#"):
        (site / "guide.html").write_text('<h1 id="present">Guide</h1>')
    with pytest.raises(ValueError, match="missing"):
        check_site(site, readme)


def test_relative_absolute_encoded_and_directory_links(documentation: tuple[Path, Path]) -> None:
    """Resolve nested paths, queries, encoded filenames, and directory indexes."""
    site, readme = documentation
    (site / "guides").mkdir()
    (site / "guides/index.html").write_text(
        '<a href="../index.html#start">Home</a><a href="?view=all#details">Here</a>'
        '<h2 id="details">Details</h2>'
    )
    (site / "space name.html").write_text('<h1 id="résumé">Encoded</h1>', encoding="utf-8")
    (site / "index.html").write_text(
        '<h1 id="start">Start</h1><a href="guides/">Guide</a>'
        '<a href="space%20name.html?view=all#r%C3%A9sum%C3%A9">Encoded</a>'
        f'<a href="{SITE}guides/#details">Absolute</a>'
        '<a href="/iperf3-lib/guides/#details">Root relative</a>'
    )
    assert check_site(site, readme) == 7


@pytest.mark.parametrize(
    "link",
    ["../outside.html", "%2e%2e/outside.html", "%2Foutside.html", "..%5Coutside.html"],
)
def test_paths_cannot_escape_site(documentation: tuple[Path, Path], link: str) -> None:
    """Reject traversal even when it is encoded or uses Windows separators."""
    site, readme = documentation
    (site.parent / "outside.html").write_text("Outside")
    (site / "index.html").write_text(f'<a href="{link}">Outside</a>')
    with pytest.raises(ValueError, match="escapes the site|backslash"):
        check_site(site, readme)


def test_external_urls_are_not_fetched(documentation: tuple[Path, Path]) -> None:
    """Skip other domains, schemes, and absolute links to another hosted project."""
    site, readme = documentation
    (site / "index.html").write_text(
        '<h1 id="start">Start</h1><a href="https://example.invalid/absent">External</a>'
        '<a href="//example.invalid/absent">External</a><a href="mailto:a@example.org">Mail</a>'
        '<a href="https://dariuszpanas.github.io/another-project/">Another project</a>'
        '<img src="data:image/png;base64,aGVsbG8=">'
    )
    assert check_site(site, readme) == 1


@pytest.mark.parametrize("link", ["CONTRIBUTING.md", "/guide/", "http://example.org/", "#absent"])
def test_readme_links_must_work_on_pypi(documentation: tuple[Path, Path], link: str) -> None:
    """Fail links that depend on a repository checkout or a missing README heading."""
    site, readme = documentation
    readme.write_text(f"# Package\n\n[Link]({link})", encoding="utf-8")
    with pytest.raises(ValueError, match="README:"):
        check_site(site, readme)


def test_readme_own_anchors_and_images(documentation: tuple[Path, Path]) -> None:
    """Validate package description headings and its same-site image assets."""
    site, readme = documentation
    (site / "logo.svg").write_text("<svg></svg>")
    readme.write_text(f"# Package\n\n[Top](#package)\n\n![Logo]({SITE}logo.svg)", encoding="utf-8")
    assert check_site(site, readme) == 1
    (site / "logo.svg").unlink()
    with pytest.raises(ValueError, match="README: missing link target"):
        check_site(site, readme)


def test_readme_documentation_fragments_are_checked(documentation: tuple[Path, Path]) -> None:
    """Check the rendered README against the same site anchor index."""
    site, readme = documentation
    readme.write_text(f"[Docs]({SITE}#missing)", encoding="utf-8")
    with pytest.raises(ValueError, match="README: missing fragment target"):
        check_site(site, readme)


def test_empty_site_fails(tmp_path: Path) -> None:
    """Require a documentation build before validating links."""
    with pytest.raises(ValueError, match="build the documentation"):
        check_site(tmp_path / "site", tmp_path / "README.md")


@pytest.mark.parametrize(
    "site_url", ["http://example.org/", "https://example.org/docs", SITE + "?x=1"]
)
def test_invalid_site_url_fails(documentation: tuple[Path, Path], site_url: str) -> None:
    """Require a canonical HTTPS base URL without a query or fragment."""
    with pytest.raises(ValueError, match="site URL"):
        check_site(*documentation, site_url=site_url)
