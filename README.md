# jahia-docker-mvn-cache

Docker images containing a warmed up maven cache. Aimed at reducing the time it takes to fetch individual maven artifacts

## Strategy

- Create a base image containing the maven cache
- Build each docker image, import the content of the maven cache

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
