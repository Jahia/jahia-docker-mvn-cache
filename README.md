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

`Dockerfile-mvn` declares two lists of versions, because the two warmup steps do not cost the same.

`JAHIA_CORE_VERSIONS` holds tags that `scripts/warm-maven-cache.sh` checks out in
[Jahia/jahia-private](https://github.com/Jahia/jahia-private). Each tag is resolved as a full
reactor, which takes about 3 minutes. The result is a wide set of the third-party artifacts that
the product depends on. Keep this list short and keep it on the recent release lines.

`JAHIA_MODULE_PARENT_VERSIONS` holds versions of the `org.jahia.modules:jahia-modules` parent.
Each entry is resolved through a throwaway pom that declares the parent. The step downloads the
parent pom, the plugins it pins, and `org.jahia.server:jahia-impl` and `jahia-taglib` at the same
version. This is the chain that a module build walks, so this list is the one that decides whether
a module repository gets a cache hit.

The parent step runs on the union of both lists, plus the version on the default branch of
`Jahia/jahia-private`. A version that appears in both lists is resolved once.

### How to choose a version for the parent list

A version belongs in `JAHIA_MODULE_PARENT_VERSIONS` when module repositories declare it. To
recount, read the `<parent>` version in the root `pom.xml` of each `Jahia` module repository, and
keep the repositories that are not archived. The list holds every version that at least 5 of those
repositories declare. It also holds the versions that were already warmed, and the
patch versions of the current release line.

The tags of `Jahia/jahia-private` are not the right source for this list. Most tags are the parent
of no module repository, and a module can declare a parent version that was never a product tag.

### When a version cannot be resolved

`scripts/warm-maven-cache.sh` reports a version it cannot resolve and continues. The end of the
build log lists every version that was not warmed, and the Actions summary shows a warning for
each one. A build on such a version still succeeds, and it fetches from the remote repository
instead of the cache.

Two failures do stop the image build: the clone of `Jahia/jahia-private`, and the resolution of the
version on its default branch.

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
