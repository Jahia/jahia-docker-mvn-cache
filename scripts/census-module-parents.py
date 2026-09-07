#!/usr/bin/env python3
"""Rewrite module-parent-versions.txt from what the repositories using this image declare.

The population is the repositories in the Jahia organisation carrying one of REQUIRED_TOPICS.
For each of them the script reads the root pom.xml on the default branch and on every
maintenance branch, and keeps the version of the org.jahia.modules:jahia-modules parent. A
repository with no such pom declares nothing and drops out on its own, which is how the
JavaScript repositories and the non-module repositories leave.

A topic is used rather than a search for the workflows that name this image: the workflow file
names are a moving target, and code search is the one call here with a per-minute limit. The
cost is that a repository whose topics are wrong is invisible to the census.

A version is warmed when at least MIN_REPOSITORIES repositories declare it, when it is at or
above FLOOR, and when it is published.

Needs GITHUB_TOKEN with read access to the organisation. Writes the file given as the first
argument, or module-parent-versions.txt next to this script's parent directory.
"""

import base64
import collections
import datetime
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

# --- Configuration -----------------------------------------------------------
# The four levers over how many versions the image warms. Each version kept adds roughly 30 MB
# to the compressed image, which every job downloads on every run, so these decide its size more
# than anything else here. A version left out is not unavailable: the build resolves it from
# Nexus instead, which is slower but works.

# Versions below this are dropped. The maintenance branches of the older modules still declare
# 7.x and 6.6.x parents, for lines that are no longer released.
FLOOR = (8, 0, 0, 0)

# How many distinct repositories must declare a version before it is warmed. At 1 a single
# repository is enough: that repository would otherwise download the parent chain on every pull
# request and every nightly run, which is a poor trade against one version's worth of image.
MIN_REPOSITORIES = 1

# A maintenance branch counts only when its last commit is this recent, because a branch nobody
# touches is a branch nobody builds. The default branch always counts whatever its age, since
# that is what pull requests are built against. None to count every branch.
ACTIVE_WITHIN_DAYS = 90

# Only repositories carrying one of these topics are read. This is the whole population, so a
# repository whose topics are missing or wrong is not censused at all.
REQUIRED_TOPICS = ["product"]

# --- Constants ---------------------------------------------------------------
# What this script reads, not what it decides.

ORG = "Jahia"
NEXUS = "https://devtools.jahia.com/nexus/content/repositories/jahia-releases"

# One branch and the commit it sits at. `day` is a UTC YYYY-MM-DD, which is what the file states.
Ref = collections.namedtuple("Ref", "name commit day is_default")

MAINTENANCE_BRANCH = re.compile(r"^[0-9]+(_[0-9]+)*_x$")
PARENT_BLOCK = re.compile(r"<parent>(.*?)</parent>", re.S)
ARTIFACT_ID = re.compile(r"<artifactId>\s*(.*?)\s*</artifactId>")
VERSION = re.compile(r"<version>\s*(.*?)\s*</version>")

TOKEN = os.environ.get("GITHUB_TOKEN", "")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}


def retry_wait(error, attempt):
    """Seconds to wait before retrying, or None when the error is not worth retrying.

    A primary rate limit answers 403 with x-ratelimit-remaining at zero and a reset time. A
    secondary one answers 403 or 429 with retry-after and a remaining that is still positive, so
    reading only the first leaves the census retrying nothing on the limit it actually hits. A
    5xx is the server, and is worth one more try.
    """
    after = error.headers.get("retry-after")
    if after:
        try:
            return max(1, int(after))
        except ValueError:                     # a date rather than a number of seconds
            return 60
    if error.code in (403, 429) and error.headers.get("x-ratelimit-remaining") == "0":
        reset = int(error.headers.get("x-ratelimit-reset", 0))
        return max(1, reset - int(time.time()) + 1)
    if error.code >= 500:
        return 2 ** attempt
    return None


def api(path, params=None, attempts=4):
    """One API call, retrying what deserves it and raising everything else.

    Every caller of this treats a raised error as fatal on purpose. A caller that read a failure
    as "no more results" or "no such file" would census a smaller organisation without saying so,
    and drop versions that are still in use.
    """
    url = f"https://api.github.com{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            return json.load(urllib.request.urlopen(request, timeout=60))
        except urllib.error.HTTPError as error:
            wait = retry_wait(error, attempt)
            if wait is None or attempt == attempts - 1:
                raise
            print(f"  {error.code} on {path}, waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
        except urllib.error.URLError as error:  # DNS, reset connection, timeout
            if attempt == attempts - 1:
                raise
            print(f"  {error.reason} on {path}, retrying", file=sys.stderr)
            time.sleep(2 ** attempt)


def consumer_repositories():
    """Repositories carrying one of REQUIRED_TOPICS.

    Whether each one actually declares the parent is settled later, by reading its pom.
    """
    repositories = set()
    for topic in REQUIRED_TOPICS:
        for page in range(1, 11):
            # A failure here raises. Writing the file from a partial search would drop versions
            # that are still in use.
            found = api("/search/repositories",
                        {"q": f"org:{ORG} topic:{topic}", "per_page": 100, "page": page})
            items = found.get("items", [])
            repositories.update(item["full_name"] for item in items)
            if len(items) < 100:
                break
    return sorted(repositories)


def days_since(day):
    """Whole days between a YYYY-MM-DD day and today, both UTC."""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    return (today - datetime.date.fromisoformat(day)).days


def last_commit(repository, ref):
    """The short commit a ref sits at and the UTC day it was made, or None.

    The branch listing carries the sha but not its date, so this is a call per ref. Maintenance
    branches need the date for ACTIVE_WITHIN_DAYS anyway; the default branch needs it only for
    the file, which is why the file states a day and not a time.
    """
    try:
        commit = api(f"/repos/{repository}/branches/{ref}").get("commit", {})
    except urllib.error.HTTPError as error:
        if error.code != 404:                   # anything else is not "no such branch"
            raise
        return None
    day = commit.get("commit", {}).get("committer", {}).get("date", "")
    if not commit.get("sha") or not day:
        return None
    return Ref(ref, commit["sha"][:7], day[:10], False)


def refs_of(repository):
    """The default branch and the maintenance branches still being committed to.

    A maintenance branch whose last commit is older than ACTIVE_WITHIN_DAYS is left out. The
    default branch is always kept, whatever its age.
    """
    try:
        default = api(f"/repos/{repository}")["default_branch"]
        branches = api(f"/repos/{repository}/branches", {"per_page": 100})
    except urllib.error.HTTPError as error:
        if error.code != 404:                   # anything else is not "no such repository"
            raise
        return []
    maintenance = sorted(b["name"] for b in branches if MAINTENANCE_BRANCH.match(b["name"]))
    refs = []
    for name in [default] + maintenance:
        ref = last_commit(repository, name)
        if ref is None:
            continue
        if name == default:
            refs.append(ref._replace(is_default=True))
        elif ACTIVE_WITHIN_DAYS is None or days_since(ref.day) <= ACTIVE_WITHIN_DAYS:
            refs.append(ref)
    return refs


def parent_version(repository, ref):
    """The jahia-modules parent version declared by the root pom, or None."""
    try:
        content = api(f"/repos/{repository}/contents/pom.xml", {"ref": ref})
    except urllib.error.HTTPError as error:
        if error.code != 404:                   # anything else is not "no pom"
            raise
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
        for ref in refs_of(repository):
            version = parent_version(repository, ref.name)
            if version:
                found.add((version, ref))
        return repository, found

    declared = collections.defaultdict(set)
    with ThreadPoolExecutor(16) as pool:
        for repository, pairs in pool.map(scan, repositories):
            name = repository.split("/", 1)[1]
            for version, ref in pairs:
                declared[version].add((name, ref))
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
    for name, ref in entries:
        grouped[name].append((0 if ref.is_default else 1, branch_numbers(ref.name), ref))
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

    # An empty result is not a valid answer, it is a failed run: every repository read badly, or
    # none was read at all. Written out it would be a file proposing that nothing be warmed.
    if not kept:
        sys.exit("no version survived the filters; refusing to write an empty census")

    lines = [
        "# Versions of the org.jahia.modules:jahia-modules parent that the Maven cache is warmed for.",
        "#",
        "# Each block is a version on a line of its own, then the number of repositories that",
        "# declare it, then each of those repositories with the branches that declare it listed",
        "# underneath, each branch with the commit it sits at and the UTC day of that commit.",
        "# Only the version lines carry data, so a repository moving between versions never",
        "# touches a line the build reads.",
        "#",
        "# This file is maintained by .github/workflows/census-module-parents.yml, which reads the",
        "# parent version declared by every repository carrying one of the topics the census asks",
        "# for, on its default branch and on each maintenance branch still being committed to. Edit",
        "# the file by hand only for a version the census cannot see, because the next run rewrites it.",
        "#",
        "# Adding a version costs about 30 MB of compressed image, paid on every pull by every job.",
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
            lines += [f"#    - {ref.name} ({ref.commit} - {ref.day})" for ref in refs]
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
