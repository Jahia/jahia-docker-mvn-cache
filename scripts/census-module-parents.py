#!/usr/bin/env python3
"""Rewrite module-parent-versions.txt from what the repositories using this image declare.

The population is the repositories in the Jahia organisation whose workflows use this image,
either by naming it or by calling a jahia-modules-action reusable workflow that names it. For
each of them the script reads the root pom.xml on the default branch and on every maintenance
branch, and keeps the version of the org.jahia.modules:jahia-modules parent.

A version is warmed when at least MIN_REPOSITORIES repositories declare it, when it is at or
above FLOOR, and when it is published. A repository with no root pom.xml is skipped, which is
how the JavaScript repositories drop out.

Needs GITHUB_TOKEN with read access to the organisation. Writes the file given as the first
argument, or module-parent-versions.txt next to this script's parent directory.
"""

import base64
import collections
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ORG = "Jahia"
FLOOR = (8, 0, 0, 0)
MIN_REPOSITORIES = 1
NEXUS = "https://devtools.jahia.com/nexus/content/repositories/jahia-releases"

# Every workflow that puts a job inside this image, or warms a runner from it.
CONSUMER_QUERIES = [
    f"org:{ORG} jahia-docker-mvn-cache",
    f"org:{ORG} jahia-modules-action/.github/workflows/reusable-on-code-change.yml",
    f"org:{ORG} jahia-modules-action/.github/workflows/reusable-release-module.yml",
    f"org:{ORG} jahia-modules-action/.github/workflows/reusable-sonar-scan.yml",
]

MAINTENANCE_BRANCH = re.compile(r"^[0-9]+(_[0-9]+)*_x$")
PARENT_BLOCK = re.compile(r"<parent>(.*?)</parent>", re.S)
ARTIFACT_ID = re.compile(r"<artifactId>\s*(.*?)\s*</artifactId>")
VERSION = re.compile(r"<version>\s*(.*?)\s*</version>")

TOKEN = os.environ.get("GITHUB_TOKEN", "")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}


def api(path, params=None, attempts=4):
    """One API call, waiting out a rate limit rather than returning a short answer.

    Code search allows 10 requests a minute, and the census needs about 8 of them. Two runs in
    the same minute therefore hit the limit, which GitHub answers with 403 and a reset time. A
    caller that treats that as "no more results" silently reads a smaller organisation.
    """
    url = f"https://api.github.com{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            return json.load(urllib.request.urlopen(request, timeout=60))
        except urllib.error.HTTPError as error:
            rate_limited = error.code in (403, 429) and error.headers.get("x-ratelimit-remaining") == "0"
            if not rate_limited or attempt == attempts - 1:
                raise
            reset = int(error.headers.get("x-ratelimit-reset", 0))
            wait = max(1, reset - int(time.time()) + 1)
            print(f"  rate limited, waiting {wait}s", file=sys.stderr)
            time.sleep(wait)


def consumer_repositories():
    """Repositories whose workflow files reach this image."""
    repositories = set()
    for query in CONSUMER_QUERIES:
        for page in range(1, 11):
            # A failure here raises. Writing the file from a partial search would drop versions
            # that are still in use.
            found = api("/search/code", {"q": query, "per_page": 100, "page": page})
            items = found.get("items", [])
            for item in items:
                if item["path"].startswith(".github/workflows/"):
                    repositories.add(item["repository"]["full_name"])
            if len(items) < 100:
                break
    return sorted(repositories)


def refs_of(repository):
    """The default branch, plus every maintenance branch."""
    try:
        default = api(f"/repos/{repository}")["default_branch"]
        branches = api(f"/repos/{repository}/branches", {"per_page": 100})
    except urllib.error.HTTPError:
        return []
    maintenance = [b["name"] for b in branches if MAINTENANCE_BRANCH.match(b["name"])]
    return [default] + sorted(maintenance)


def parent_version(repository, ref):
    """The jahia-modules parent version declared by the root pom, or None."""
    try:
        content = api(f"/repos/{repository}/contents/pom.xml", {"ref": ref})
    except urllib.error.HTTPError:
        return None
    pom = base64.b64decode(content.get("content", "")).decode("utf-8", "replace")
    block = PARENT_BLOCK.search(pom)
    if not block:
        return None
    artifact = ARTIFACT_ID.search(block.group(1))
    version = VERSION.search(block.group(1))
    if not artifact or not version or artifact.group(1) != "jahia-modules":
        return None
    return version.group(1)


def as_tuple(version):
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return None


def is_published(version):
    """True when the core jar of that version is in the release repository.

    A version that cannot be checked is kept. Losing the network must not silently shrink the
    warmed set, and the build reports a version it fails to resolve anyway.
    """
    url = f"{NEXUS}/org/jahia/server/jahia-impl/{version}/jahia-impl-{version}.jar"
    request = urllib.request.Request(url, method="HEAD")
    try:
        urllib.request.urlopen(request, timeout=30)
        return True
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        return True
    except Exception as error:  # unreachable repository, DNS, timeout
        print(f"  cannot check {version} against the release repository: {error}", file=sys.stderr)
        return True


def collect():
    repositories = consumer_repositories()
    print(f"{len(repositories)} repositories use this image", file=sys.stderr)

    def scan(repository):
        found = set()
        refs = refs_of(repository)
        # refs_of puts the default branch first, and it is not always called main.
        default = refs[0] if refs else None
        for ref in refs:
            version = parent_version(repository, ref)
            if version:
                found.add((version, ref, ref == default))
        return repository, found

    declared = collections.defaultdict(set)
    with ThreadPoolExecutor(16) as pool:
        for repository, triples in pool.map(scan, repositories):
            name = repository.split("/", 1)[1]
            for version, ref, is_default in triples:
                declared[version].add((name, ref, is_default))
    return repositories, declared


def branch_numbers(ref):
    """The numbers in a maintenance branch name, so 9_x sorts before 10_x rather than after it."""
    return tuple(int(n) for n in re.findall(r"\d+", ref))


def by_repository(entries):
    """Group (name, ref, is_default) entries as name -> refs.

    A repository whose default and maintenance branches agree on a version contributes one entry
    per branch, so the entries are grouped before they are counted: counting them directly would
    count that repository more than once, and would let one repository on its own satisfy
    MIN_REPOSITORIES. Repositories come out alphabetically, and each one's default branch comes
    before its maintenance branches, which are ordered by their numbers.
    """
    grouped = collections.defaultdict(list)
    for name, ref, is_default in entries:
        grouped[name].append((0 if is_default else 1, branch_numbers(ref), ref))
    return {name: [ref for *_, ref in sorted(refs)] for name, refs in sorted(grouped.items())}


def render(repositories, declared):
    kept, dropped = {}, []
    for version, entries in declared.items():
        count = len(by_repository(entries))
        parts = as_tuple(version)
        if parts is None or version.endswith("SNAPSHOT"):
            dropped.append((version, count, "not a released version"))
        elif parts < FLOOR:
            dropped.append((version, count, f"below {'.'.join(map(str, FLOOR))}"))
        elif count < MIN_REPOSITORIES:
            dropped.append((version, count, f"fewer than {MIN_REPOSITORIES} repositories"))
        elif not is_published(version):
            dropped.append((version, count, "not in the release repository"))
        else:
            kept[version] = sorted(entries)

    for version, count, why in sorted(dropped, key=lambda row: -row[1]):
        print(f"  dropped {version} ({count} repositories): {why}", file=sys.stderr)

    lines = [
        "# Versions of the org.jahia.modules:jahia-modules parent that the Maven cache is warmed for.",
        "#",
        "# Each block is a version on a line of its own, then the number of repositories that",
        "# declare it, then each of those repositories with the branches that declare it listed",
        "# underneath. Only the version lines carry data, so a repository moving between versions",
        "# never touches a line the build reads.",
        "#",
        "# This file is maintained by .github/workflows/census-module-parents.yml, which reads the parent",
        "# version declared by every repository whose CI uses this image, on its default branch and on",
        "# each of its maintenance branches. Edit the file by hand only for a version the census",
        "# cannot see, because the next run rewrites it.",
        "#",
        "# Adding a version costs about 24 MB of compressed image, paid on every pull by every job.",
        "#",
        f"# {len(repositories)} repositories were read, and {len(kept)} versions are warmed.",
        "",
    ]
    for version in sorted(kept, key=lambda v: as_tuple(v), reverse=True):
        grouped = by_repository(kept[version])
        noun = "repository" if len(grouped) == 1 else "repositories"
        lines.append(version)
        lines.append(f"# {len(grouped)} {noun}" + (":" if grouped else ""))
        for name, refs in grouped.items():
            lines.append(f"#  - {name}")
            lines += [f"#    - {ref}" for ref in refs]
        lines.append("")
    return "\n".join(lines) + "\n"


def main():
    if not TOKEN:
        sys.exit("GITHUB_TOKEN is not set")
    target = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else \
        pathlib.Path(__file__).resolve().parent.parent / "module-parent-versions.txt"
    repositories, declared = collect()
    target.write_text(render(repositories, declared))
    print(f"wrote {target}", file=sys.stderr)


if __name__ == "__main__":
    main()
