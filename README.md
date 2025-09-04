# jahia-docker-mvn-cache

Docker images containing a warmed up maven cache. Aimed at reducing the time it takes to fetch individual maven artifacts during CI build steps.

This repository serves as a replacement for https://github.com/Jahia/cimg-mvn-cache.
It is heavily inspired by work done in https://github.com/timbru31/docker-java-node

Images are pushed to this GitHub Packages repository: https://github.com/Jahia/jahia-docker-mvn-cache/pkgs/container/jahia-docker-mvn-cache

## Repository organization and build flow

Multiple images are built, with different JDK versions. To avoid having to run the "slow" `mvn dependency:resolve` multiple time, a first "default" image is built entirely.

Then when building subsequent images, the .m2 folder is fetched directly from a fully built default image using a multi-stage Dockerfile.

Default versions and additional images are defined in the `build-and-push.yml` GitHub Action worklow.

High-level flow (example using JDK 17 as the default):

```
                         ┌──────────────────────────────────────┐
                         │  Dockerfile (17-jdk-alpine)          │  (fast)
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
  │  Dockerfile-base (8-jdk-alpine)      │  (fast)      │  Dockerfile-base (11-jdk-alpine)     │  (fast)
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

## Build image locally

From an ARM64 host, build a base image (name: `ghcr.io/jahia/jahia-docker-mvn-cache:eclipse-temurin-11-jdk-alpine-node`)

```bash
docker buildx build \
  --platform linux/amd64 \
  --build-arg REFRESHED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  -t ghcr.io/jahia/jahia-docker-mvn-cache:eclipse-temurin-11-jdk-alpine-node \
  -f Dockerfile-11-jdk-alpine-node \
  --push \
  .
```

Once the base image is ready, build the maven cache image (name: `ghcr.io/jahia/jahia-docker-mvn-cache:11-jdk-alpine-node-mvn`)

```bash
docker buildx build \
  --platform linux/amd64 \
  --load \
  --ssh default \
  --pull \
  -t ghcr.io/jahia/jahia-docker-mvn-cache:11-jdk-alpine-node-mvn \
  -f Dockerfile \
  .
```

Finally, open a bash session inside the container

```bash
docker run --rm -it \
  --platform linux/amd64 \
  --entrypoint /bin/sh \
  ghcr.io/jahia/jahia-docker-mvn-cache:11-jdk-alpine-node-mvn
```
