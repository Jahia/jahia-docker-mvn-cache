ARG BASE_TAG=17-jdk-alpine

FROM eclipse-temurin:$BASE_TAG
LABEL maintainer="Jahia"

ARG REFRESHED_AT
ENV REFRESHED_AT=$REFRESHED_AT

ARG MAVEN_VERSION=3.9.11
ENV MAVEN_HOME=/opt/maven \
  MAVEN_CONFIG=/root/.m2

RUN echo @new-stable https://dl-cdn.alpinelinux.org/alpine/latest-stable/community >> /etc/apk/repositories \
  && echo @new-stable https://dl-cdn.alpinelinux.org/alpine/latest-stable/main >> /etc/apk/repositories \
  && apk -U upgrade \
  && apk add --no-cache \
    nodejs@new-stable \
    npm@new-stable \
    yarn \
    curl \
    ca-certificates \
    git \
    openssh-client \
    bash \
    tar


# Install Maven (download official binary to avoid pulling another JRE)
RUN set -eux; \
  curl -fsSL -o /tmp/apache-maven.tar.gz \
    https://archive.apache.org/dist/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz; \
  mkdir -p /opt; \
  tar -xzf /tmp/apache-maven.tar.gz -C /opt; \
  ln -s /opt/apache-maven-${MAVEN_VERSION} ${MAVEN_HOME}; \
  ln -s ${MAVEN_HOME}/bin/mvn /usr/local/bin/mvn; \
  rm -f /tmp/apache-maven.tar.gz

USER root

COPY maven.settings.xml .

# Ensure ssh utilities are available and record github.com host key
RUN mkdir -p -m 0700 /root/.ssh \
 && ssh-keyscan -T 20 -t rsa,ecdsa,ed25519 github.com >> /root/.ssh/known_hosts
  