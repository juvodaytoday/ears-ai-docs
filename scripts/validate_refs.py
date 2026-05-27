#!/usr/bin/env python3
"""Validate internal references in this Mintlify docs repo.

Run locally from the repo root (no dependencies, Python 3 stdlib only):

    python scripts/validate_refs.py            # validate the repo this script lives in
    python scripts/validate_refs.py /some/path # validate an explicit repo root

Exit codes: 0 = no errors (warnings allowed), 1 = errors found, 2 = could not run.

Rules enforced:
  1. Anchor URLs (mint.json "anchors"): valid if the URL resolves to a real
     page file, OR (bare-directory form) at least one nav page has it as a
     path prefix. Mintlify v1 auto-routes bare-directory anchors to the first
     matching nav page, so they are intentionally allowed.
  2. Navigation: every page listed in mint.json "navigation" resolves to an
     .mdx/.md file (error). Content files not referenced by nav are reported
     as orphan warnings, not errors (some files may be intentionally unlisted).
  3. Internal links in .mdx content ([text](path) and href="path"): the path
     must resolve to a real file in the repo. External (http/https/mailto/tel)
     and pure #anchors are skipped; #hash and ?query are stripped before
     resolving (hash targets are out of scope for v1).
  4. Frontmatter: every .mdx file must have a fenced YAML frontmatter block
     whose single-line scalar values have balanced quotes (lightweight check
     that catches unterminated quotes, not a full YAML parse).
"""

import re
import sys
from pathlib import Path

PAGE_EXTS = (".mdx", ".md")
IMAGE_OR_ASSET_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".pdf", ".json", ".css", ".js", ".mp4", ".zip", ".txt",
}
EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "tel:")

MD_LINK_RE = re.compile(r"(!?)\[[^\]]*\]\(\s*([^)\s]+)")
HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""")
INLINE_CODE_RE = re.compile(r"`[^`]*`")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


def load_mint_json(root):
    import json
    path = root / "mint.json"
    if not path.exists():
        raise FileNotFoundError(f"mint.json not found at {path}")
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def collect_nav_pages(mint):
    """Return the set of page-path strings referenced in mint.json navigation."""
    pages = set()

    def walk(item):
        if isinstance(item, str):
            pages.add(item)
        elif isinstance(item, dict):
            for sub in item.get("pages", []):
                walk(sub)
        elif isinstance(item, list):
            for sub in item:
                walk(sub)

    walk(mint.get("navigation", []))
    return pages


def collect_anchor_urls(mint):
    urls = []
    for anchor in mint.get("anchors", []):
        url = anchor.get("url")
        if url:
            urls.append((anchor.get("name", "?"), url))
    return urls


def _strip_target(target):
    """Drop a #fragment / ?query and surrounding slashes from a URL path."""
    target = target.split("#", 1)[0].split("?", 1)[0].strip()
    return target


def resolve_page(root, target, base_dir=None):
    """True if a docs URL/path resolves to a real content file.

    Absolute targets (leading /) resolve from the repo root; relative targets
    resolve from base_dir. Handles .mdx/.md, directory /index, and trailing
    slashes the way Mintlify routes pages.
    """
    target = _strip_target(target)
    if target == "" or target == "/":
        target = "index"

    if target.startswith("/"):
        rel = target.lstrip("/")
        base = root
    else:
        rel = target
        base = base_dir or root

    rel = rel.rstrip("/")
    if rel == "":
        rel = "index"

    candidates = []
    # If the path already names a file with a known page extension, check it.
    if rel.endswith(PAGE_EXTS):
        candidates.append(rel)
    else:
        for ext in PAGE_EXTS:
            candidates.append(rel + ext)
            candidates.append(f"{rel}/index{ext}")

    for cand in candidates:
        if (base / cand).is_file():
            return True
    return False


def nav_page_resolves(root, page):
    """Nav entries are bare page paths (no leading slash, no extension)."""
    for ext in PAGE_EXTS:
        if (root / f"{page}{ext}").is_file():
            return True
        if (root / page / f"index{ext}").is_file():
            return True
    return False


def check_nav_pages_exist(root, nav_pages):
    errors = []
    for page in sorted(nav_pages):
        if not nav_page_resolves(root, page):
            errors.append(f"navigation references missing page: {page}")
    return errors


def iter_content_files(root, exts):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in exts:
            continue
        parts = path.relative_to(root).parts
        if parts and parts[0] in (".git", ".github", "node_modules"):
            continue
        yield path


def page_keys_for_file(root, path):
    """Nav-path forms a content file may be referenced by.

    An index file can be listed either as "dir/index" or as the bare "dir",
    so both are accepted.
    """
    rel = path.relative_to(root).with_suffix("")
    keys = {rel.as_posix()}
    if rel.name == "index":
        keys.add(rel.parent.as_posix())
    return keys


def check_orphans(root, nav_pages):
    warnings = []
    for path in iter_content_files(root, (".mdx",)):
        if not (page_keys_for_file(root, path) & nav_pages):
            warnings.append(f"file not referenced in navigation: {path.relative_to(root)}")
    return warnings


def check_anchor_urls(root, anchors, nav_pages):
    errors = []
    for name, url in anchors:
        clean = _strip_target(url).lstrip("/").rstrip("/")
        if resolve_page(root, url):
            continue
        prefix = clean + "/"
        if any(page == clean or page.startswith(prefix) for page in nav_pages):
            continue
        errors.append(
            f'anchor "{name}" url "{url}" does not resolve to a file '
            f"or match any nav page prefix"
        )
    return errors


def extract_links(text):
    """Yield (line_number, target) for markdown links and href attributes.

    Markdown image embeds (![...](...)) are skipped, as is anything inside a
    fenced code block or an inline code span (those hold example markup, not
    real links).
    """
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = INLINE_CODE_RE.sub("", line)
        for bang, target in MD_LINK_RE.findall(line):
            if bang == "!":
                continue
            yield lineno, target
        for target in HREF_RE.findall(line):
            yield lineno, target


def check_internal_links(root):
    errors = []
    for path in iter_content_files(root, (".mdx",)):
        text = path.read_text(encoding="utf-8")
        base_dir = path.parent
        for lineno, raw in extract_links(text):
            target = raw.strip()
            if target.lower().startswith(EXTERNAL_PREFIXES):
                continue
            if target.startswith("#"):
                continue
            stripped = _strip_target(target)
            if stripped == "":
                continue
            suffix = Path(stripped).suffix.lower()
            if suffix in IMAGE_OR_ASSET_EXTS:
                continue
            if not resolve_page(root, target, base_dir=base_dir):
                rel = path.relative_to(root)
                errors.append(f"{rel}:{lineno} broken internal link: {target}")
    return errors


def check_frontmatter(root):
    errors = []
    for path in iter_content_files(root, (".mdx",)):
        rel = path.relative_to(root)
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines or lines[0].strip() != "---":
            errors.append(f"{rel}: missing frontmatter (file must start with '---')")
            continue
        close_idx = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                close_idx = i
                break
        if close_idx is None:
            errors.append(f"{rel}: unterminated frontmatter (no closing '---')")
            continue
        for i in range(1, close_idx):
            problem = _unbalanced_quote(lines[i])
            if problem:
                errors.append(f"{rel}:{i + 1} frontmatter {problem}: {lines[i].strip()}")
    return errors


def _unbalanced_quote(line):
    """Return a problem description if a single-line scalar has an open quote.

    Only flags values that *start* with a quote but do not *end* with the
    matching quote — the unterminated-quote bug class. Block scalars (| >),
    flow collections ([ {), list items (-) and unquoted values are ignored to
    avoid false positives from apostrophes in plain text.
    """
    m = re.match(r"^\s*[\w.\-]+\s*:\s*(.*)$", line)
    if not m:
        return None
    value = m.group(1).strip()
    if not value or value[0] not in "\"'":
        return None
    if value[0] in "|>[{":
        return None
    quote = value[0]
    if len(value) < 2 or value[-1] != quote:
        return f"unterminated {quote} quote"
    return None


def main(argv):
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parent.parent
    try:
        mint = load_mint_json(root)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: could not load mint.json: {exc}", file=sys.stderr)
        return 2

    nav_pages = collect_nav_pages(mint)
    anchors = collect_anchor_urls(mint)

    error_groups = [
        ("Navigation pages", check_nav_pages_exist(root, nav_pages)),
        ("Anchor URLs", check_anchor_urls(root, anchors, nav_pages)),
        ("Internal links", check_internal_links(root)),
        ("Frontmatter", check_frontmatter(root)),
    ]
    warning_groups = [
        ("Orphan files (not in navigation)", check_orphans(root, nav_pages)),
    ]

    total_errors = sum(len(items) for _, items in error_groups)
    total_warnings = sum(len(items) for _, items in warning_groups)

    print(f"Validating docs at: {root}")
    print(f"  nav pages: {len(nav_pages)} | anchors: {len(anchors)}\n")

    for title, items in error_groups:
        if items:
            print(f"ERRORS - {title} ({len(items)}):")
            for item in items:
                print(f"  - {item}")
            print()

    for title, items in warning_groups:
        if items:
            print(f"WARNINGS - {title} ({len(items)}):")
            for item in items:
                print(f"  - {item}")
            print()

    if total_errors:
        print(f"FAILED: {total_errors} error(s), {total_warnings} warning(s).")
        return 1
    print(f"OK: 0 errors, {total_warnings} warning(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
