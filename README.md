# jahia-docker-mvn-cache

Docker images containing a warmed up maven cache. Aimed at reducing the time it takes to fetch individual maven artifacts

## Repository organization and build flow

We build multiple independent images while sharing heavy build work (warming the Maven cache) across them.

- Base Dockerfiles per JDK: `Dockerfile-8-jdk-alpine-node`, `Dockerfile-11-jdk-alpine-node`, `Dockerfile-17-jdk-alpine-node`
- A generic base with common tooling: `Dockerfile-base`
- Cache-loader Dockerfile: `Dockerfile` (clones private sources and runs Maven to warm the cache)
- GitHub Actions workflow: `.github/workflows/build-and-push.yml`

High-level flow (example using JDK 17 as the “producer”):

```
                 ┌──────────────────────────────────────┐
                 │  Dockerfile-17-jdk-alpine-node       │  (fast)
                 │  - JDK + Node + Maven (no cache)     │
                 └───────────────┬──────────────────────┘
                                 │ build & push base image
                                 ▼
                 ┌──────────────────────────────────────┐
                 │  Dockerfile (cache loader)           │  (slow once)
                 │  - git clone + mvn dependency:resolve│
                 │  - produces warmed /home/jahia-ci/.m2│
                 └───────────────┬──────────────────────┘
                                 │ push cache-loaded image (producer)
                                 ▼
      ┌───────────────────────────┴───────────────────────────┐
      │                                                       │
┌──────────────┐                                       ┌──────────────┐
│ JDK 8 base   │                                       │ JDK 11 base  │
│ Dockerfile-8 │                                       │ Dockerfile-11│
└──────┬───────┘                                       └──────┬───────┘
       │ COPY --from=producer .m2                                │ COPY --from=producer .m2
       ▼                                                          ▼
  build/push JDK 8 image with cache                        build/push JDK 11 image with cache
```

Key idea: warm the Maven cache once in a “producer” image, then other images copy the `.m2` directory from that image instead of running Maven again.

In Dockerfiles for consumer images, use external multi-stage COPY:

```dockerfile
# syntax=docker/dockerfile:1.7
FROM ghcr.io/jahia/jahia-docker-mvn-cache:17-jdk-alpine-node-mvn AS producer

FROM eclipse-temurin:11-jdk-alpine
# ... create non-root user 1000:1000 (e.g., jahia-ci) ...
COPY --from=producer --chown=1000:1000 /home/jahia-ci/.m2 /home/jahia-ci/.m2
```

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

## CI workflow (matrix)

The GitHub Actions workflow builds the “producer” (e.g., JDK 17) first to warm the cache. Subsequent matrix builds (e.g., JDK 8, 11) create their base images and COPY the `.m2` directory from the producer image instead of re-fetching dependencies.

Benefits:

- Warm Maven cache once per run → big time savings
- Repeatable: consumers pin the producer tag (or digest)
- Lean: images remain minimal; no duplicate network traffic
