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

`.github/workflows/census-module-parents.yml` runs every day and rewrites the file. It reads
every repository in the `Jahia` organisation whose workflows use this image. In each one, the census takes the parent version from the root `pom.xml`, on the default branch and on
each maintenance branch. A repository with no root `pom.xml` is skipped, so the JavaScript
repositories drop out. Under each version the file lists the repositories that
declare it, and under each repository the branches that do, so a version can be traced to the
branch that asked for it. The count is of repositories, so it is smaller than the number of
branch lines whenever a repository declares the same version on more than one branch.

A version is kept when it is `8.0.0.0` or later and when it is not a SNAPSHOT. It is also
checked against the release repository, where `jahia-impl` must exist at that version. That last check matters. A tag of
`Jahia/jahia-private` can exist months before its artifacts are published, and a version whose
`jahia-impl` is missing warms nothing but a pom.

The census never changes the image on its own. It opens a pull request, and the build of that
pull request publishes an image whose size can be compared with the one on `main`. Each added
version costs about 24 MB of compressed image, paid on every pull by every job.

A pull request is opened only when the set of versions changes. A repository that moves from one
warmed version to another rewrites the repository lists while the warmed set stays the same, and
that alone is not worth a rebuild of the image.

The version on the default branch of `Jahia/jahia-private` is always resolved, so a repository
that builds against the current SNAPSHOT needs no entry in the file. The SNAPSHOT artifacts
themselves are removed from the cache at the end of the warmup, because they change.

To change how many versions are kept, edit `MIN_REPOSITORIES` or `FLOOR` at the top of
`scripts/census-module-parents.py`.

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
