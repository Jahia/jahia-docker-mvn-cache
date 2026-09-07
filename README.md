# jahia-docker-mvn-cache

Docker images containing a warmed up maven cache. Aimed at reducing the time it takes to fetch individual maven artifacts during CI build steps.

This repository serves as a replacement for https://github.com/Jahia/cimg-mvn-cache.
It is heavily inspired by work done in https://github.com/timbru31/docker-java-node

Images are pushed to this GitHub Packages repository: https://github.com/Jahia/jahia-docker-mvn-cache/pkgs/container/jahia-docker-mvn-cache

## Repository organization and build flow

Multiple images are built, with different JDK versions. To avoid having to run the "slow" `mvn dependency:resolve dependency:resolve-plugins` multiple time, a 
first "default" image is built entirely.

Then when building subsequent images, the .m2 folder is fetched directly from a fully built default image using a multi-stage Dockerfile.

Default versions and additional images are defined in the `build-and-push.yml` GitHub Action workflow.

High-level flow (example using JDK 17 as the default):

```
                         ┌──────────────────────────────────────┐
                         │  Dockerfile (17-jdk-resolute)           │  (fast)
                         │  - JDK + Node + Maven (no cache)     │
                         └───────────────┬──────────────────────┘
                                         │ build & push base image
                                         ▼
                         ┌──────────────────────────────────────────┐
                         │  Dockerfile-mvn (cache loader)           │  (slow once)
                         │  - git clone + mvn dependency:resolve.   │
                         │  - produces warmed /root/.m2.            │
                         └───────────────┬──────────────────────────┘
                                         │ push cache-loaded image (default)
                                         ▼
                  ┌──────────────────────┴────────────────────────────────┐
                  │                                                       │
  ┌──────────────────────────────────────┐              ┌──────────────────────────────────────┐
  │  Dockerfile      (8-jdk-resolute)       │  (fast)      │  Dockerfile      (11-jdk-resolute)      │  (fast)
  │  - JDK + Node + Maven (no cache)     │              │  - JDK + Node + Maven (no cache)     │
  └───────────────┬──────────────────────┘              └───────────────┬──────────────────────┘
                  │                                                     │
                  ▼                                                     ▼
  ┌────────────────────────────────────────┐              ┌────────────────────────────────────────┐
  │  Dockerfile-fromcache                  │  (fast)      │  Dockerfile-fromcache                  │  (fast)
  │  - copy .m2 folder from default image  │              │  - copy .m2 folder from default image  │
  └───────────────┬──────────────────────--┘              └────--───────────┬──────────────────────┘
                  │                                                         │
                  ▼                                                         ▼
      build/push JDK 8 image with cache                     build/push JDK 11 image with cache

```

Key idea: warm the Maven cache once in a default image, then other images copy the `.m2` directory from that image instead of running Maven again.

## Which Jahia versions are warmed

The warmup has two steps, and they do not pull the same artifacts, so they take their versions
from two different places.

`JAHIA_CORE_VERSIONS`, a build arg in `Dockerfile-mvn`, holds tags that
[Jahia/jahia-private](https://github.com/Jahia/jahia-private) is checked out at. Each tag is
resolved as a full product reactor, which downloads the third-party artifacts and the plugins the
product build needs. A reactor resolution never downloads a Jahia artifact, because
`jahia-impl`, `jahia-api` and `jahia-taglib` are members of that reactor. This list is decided by
hand and stays short.

`module-parent-versions.txt` holds versions of the `org.jahia.modules:jahia-modules` parent. Each
one is resolved through a throwaway pom that declares the parent. That downloads the parent pom,
the plugins it pins, and `org.jahia.server:jahia-impl` and `jahia-taglib` at the same version.
This is the chain a module build walks, so this file is what decides whether a module repository
gets a cache hit.

The parent step runs on the union of the file, the core versions and the version on the default
branch of `Jahia/jahia-private`. A version that appears twice is resolved once.

### How module-parent-versions.txt is maintained

`.github/workflows/census-module-parents.yml` runs every day and rewrites the file. It reads every
repository in the `Jahia` organisation that carries one of the topics the census asks for, and takes
the parent version from the root `pom.xml` on the default branch and on each maintenance branch. A
repository with no such pom declares nothing and drops out on its own, which is how the JavaScript
and non-module repositories leave.

The population is chosen by topic rather than by searching for the workflows that name this image.
Those workflow file names are a moving target, and code search is the one call with a per-minute
limit. The cost is that a repository whose topics are missing or wrong is invisible to the census,
and keeps building without a cache hit.

Under each version the file lists the repositories that declare it, and under each repository the
branches that do, each with the commit it sits at and the UTC day of that commit:

```
8.2.3.0
# 7 repositories:
#  - jahia-authentication
#    - main (866d279 - 2026-09-02)
#  - jcr-auth-provider
#    - main (ba07c82 - 2026-07-27)
```

So a version can be traced to the branch that asked for it, and to how recently that branch moved.
The count is of repositories, so it is smaller than the number of branch lines whenever a repository
declares the same version on more than one branch.

### What decides whether a version is warmed

Four settings at the top of `scripts/census-module-parents.py`, and they matter more than anything
else in it: each version kept adds roughly 30 MB to the compressed image, downloaded by every job on
every run. A version left out is not unavailable — the build resolves it from Nexus instead, which
is slower but works.

| setting | what it does |
|---|---|
| `FLOOR` | Versions below it are dropped. Older modules' maintenance branches still declare 7.x and 6.6.x parents, for lines no longer released. |
| `MIN_REPOSITORIES` | How many repositories must declare a version. At 1 a single repository is enough, because that repository would otherwise pay a download on every pull request and every nightly run. |
| `ACTIVE_WITHIN_DAYS` | A maintenance branch counts only if its last commit is this recent: a branch nobody touches is a branch nobody builds. The default branch always counts. |
| `REQUIRED_TOPICS` | The topics that define the population above. |

A SNAPSHOT is never warmed. A version is also checked against the release repository, where
`jahia-impl` must exist at that version, because a tag of `Jahia/jahia-private` can exist months
before its artifacts are published and a version whose `jahia-impl` is missing warms nothing but a
pom.

### How the change reaches the image

The census never changes the image on its own. It opens a pull request against `main`, and the image
is rebuilt when that lands. The pull request itself starts no build, because it is opened by the job's
own token: to measure a proposal before merging it, dispatch **On Code Change** on the proposal branch,
which tags its images after the branch rather than after a pull request number.

What the census compares is the list of versions, nothing else. The repository and branch lines are
comments, so a repository moving between warmed versions, a new declarer appearing, or a commit date
changing all leave the warmed set alone and cost no pull request and no rebuild.

That gives three outcomes:

| the versions | what happens |
|---|---|
| differ, and no pull request is open | one is opened |
| differ, and one is already open | it is updated in place, body included |
| match | any open pull request is closed, because the repositories have moved back and it would otherwise stay mergeable |

The version on the default branch of `Jahia/jahia-private` is always resolved, so a repository that
builds against the current SNAPSHOT needs no entry in the file.

### What the image warmed

Every image carries `/opt/jahia-mvn-cache-report.txt`, which lists each version the build
resolved and whether it succeeded. Read it instead of reading the version list, because a
resolution that fails leaves the version out without failing the build.

The product reactor lines also name the commit of `Jahia/jahia-private` each one was resolved
at, because they check out a tag and a tag can be moved. The parent chain lines have no commit:
they resolve Maven coordinates from the repository.

## Build image locally

From an ARM64 host, build a base image (name: `ghcr.io/jahia/jahia-docker-mvn-cache:11-jdk-resolute-node-base`)

```bash
docker buildx build \
  --platform linux/amd64 \
  --build-arg REFRESHED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --build-arg BASE_TAG="11-jdk-resolute" \
  -t ghcr.io/jahia/jahia-docker-mvn-cache:11-jdk-resolute-node-base \
  -f Dockerfile \
  --push \
  .
```

Once the base image is ready, build the maven cache image (name: `ghcr.io/jahia/jahia-docker-mvn-cache:11-jdk-resolute-mvn-loaded`)

```bash
docker buildx build \
  --platform linux/amd64 \
  --build-arg SRC_IMAGE="ghcr.io/jahia/jahia-docker-mvn-cache:11-jdk-resolute-node-base" \
  --load \
  --ssh default \
  --pull \
  -t ghcr.io/jahia/jahia-docker-mvn-cache:11-jdk-resolute-mvn-loaded \
  -f Dockerfile-mvn \
  .
```

Finally, open a bash session inside the container

```bash
docker run --rm -it \
  --platform linux/amd64 \
  --entrypoint /bin/sh \
  ghcr.io/jahia/jahia-docker-mvn-cache:11-jdk-resolute-mvn-loaded
```
