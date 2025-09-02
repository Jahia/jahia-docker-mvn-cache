ARG SRC_IMAGE=ghcr.io/jahia/jahia-docker-mvn-cache:17-jdk-alpine-node-base

FROM $SRC_IMAGE

# Create a non-root user (uid/gid 1000) with a home directory
RUN addgroup -g 1000 jahia-ci \
 && adduser -u 1000 -G jahia-ci -D -h /home/jahia-ci jahia-ci \
 && mkdir -p /home/jahia-ci \
 && chown -R jahia-ci:jahia-ci /home/jahia-ci

# Switch to non-root by default
ENV HOME=/home/jahia-ci
WORKDIR /home/jahia-ci
USER jahia-ci

# Maven settings used to warm the cache
ADD maven.settings.xml .

# Ensure ssh utilities are available and record github.com host key
RUN mkdir -p -m 0700 /home/jahia-ci/.ssh \
 && ssh-keyscan -T 20 -t rsa,ecdsa,ed25519 github.com >> /home/jahia-ci/.ssh/known_hosts

# Warm up the Maven cache from the private repository (SSH key is provided at build time)
# Fail-fast: any failing command will stop the build
RUN --mount=type=ssh,uid=1000,gid=1000 sh -lc '\
    set -euo pipefail; \
    git clone git@github.com:Jahia/jahia-private.git && \
    cd jahia-private && \
    mvn -B -s ../maven.settings.xml dependency:resolve && \
    cd .. && \
    rm -rf jahia-private'
