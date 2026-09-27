# Documentation maintenance

## Structure

The documentation follows the same Zensical and GitHub Pages pattern as
[YAGA](https://dariuszpanas.github.io/yaga/), with installation and guides first,
API reference next, and a small About section for contributing, security and
release history. Maintainer procedures, planning and design evidence belong in
`development/`, outside the published documentation tree. Navigation is explicit
in `zensical.toml`. The theme includes system, light, and dark palettes and uses
local system fonts.

| Path | Purpose |
| --- | --- |
| `docs/` | Public user guides, reference pages and stylesheet |
| `development/` | Repository-only procedures, planning and design evidence |
| `zensical.toml` | Site metadata, navigation, and theme configuration |
| `overrides/` | Unreleased banner and project-aware 404 page |
| `scripts/check_docs.py` | Built-site and rendered README link validation |
| `.github/workflows/docs.yml` | GitHub Pages build and deployment |
| `site/` | Generated output, ignored by Git |

Keep the repository README as a short project overview and entry point to these
pages. Maintain examples, supported-version tables, execution limits, and
release history in the documentation; link to them from the README instead of
copying them. The README is also the package description on PyPI.

Old roadmap, design and maintainer page URLs retain short landing pages outside
the navigation. Their existing section anchors point readers to the moved
material. Keep these pages short; add new maintainer content under
`development/`, not to the public landing pages.

## Build and preview

```bash
make install
make docs
make docs-serve
```

The direct commands, including on a host without Make, are:

```bash
uv run --frozen zensical build --strict --clean
uv run --frozen python scripts/check_docs.py
uv run --frozen zensical serve
```

Open the local address printed by the preview server. Generated page URLs use
`.html`; preview through HTTP so the bundled search worker can load. The
offline plugin is not enabled because it adds a remote worker shim.
Check both palettes and narrow screens when changing layout or navigation.

`make docs` rejects build warnings and broken local pages, assets, and anchors.
It renders the README with the PyPI renderer and checks its links into the site.
Remote availability is checked separately before a release. Keep README links
absolute so they work on GitHub and PyPI.

## GitHub Pages

Set **Settings → Pages → Build and deployment → Source** to **GitHub Actions**.
The workflow builds the site from `main`, uploads that exact artifact, and
deploys it with the `github-pages` environment. Build jobs have read access;
only the deploy job receives Pages and OIDC write permissions. Manual dispatch
also requires `main`. PRs run the build/link gate through the regular CI workflow.

The intended site URL is <https://dariuszpanas.github.io/iperf3-lib/>.
After the first deployment, check its homepage, deep links, search, 404 page,
README documentation links, and the reported deployment revision. A successful
local build does not establish that the public site is deployed.

Workflow action references are immutable SHAs updated by Dependabot. The setup
uses current stable uv, with dependency versions recorded in `uv.lock`.

## Version policy

This site documents `main`, and its banner identifies APIs that have not shipped.
Keep `changelog.md`, the homepage, and the installation guide explicit about that
boundary. The README links readers to those version-specific instructions.
At release time update the documentation together with package metadata. Until
versioned documentation is introduced, link users of older releases to their
tagged README and source rather than presenting current APIs as historical ones.
