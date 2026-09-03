#!/usr/bin/env bash
# Warm /root/.m2 with the artifacts a Jahia CI build asks for.
#
# Two steps, and they do not pull the same thing:
#   1. jahia-private is cloned and resolved at each version of JAHIA_VERSIONS. A reactor
#      resolution downloads the third-party artifacts and the plugins the product build needs.
#   2. A throwaway pom whose parent is org.jahia.modules:jahia-modules is resolved at the same
#      versions. That downloads the parent chain a module build walks.
#
# JAHIA_VERSIONS, COMMON_GOALS and MVN_CMD come in as build args, and MVN_CMD points at
# ../maven.settings.xml. Both working directories below sit one level under the build directory,
# so that relative path resolves in each of them.
#
# Failure semantics, transcribed from the command this script replaces. The clone, the cd, the
# version lookup and the resolution of the default version stop the build. Every per-version
# resolution below is tolerated, because in the original those loops sat inside an && list, where
# bash suppresses errexit, and the list ended with `|| true`.

set -euo pipefail

# buildx keeps only the first 2 MiB of a step's output, and the resolutions below produce far
# more than that, so every outcome is recorded here and a later step prints this file instead.
REPORT="${REPORT:-/opt/jahia-mvn-cache-report.txt}"   # overridable so the script can be run outside a build
# Absolute, because the steps below change directory and then delete those directories.
[[ ${REPORT} == /* ]] || REPORT="${PWD}/${REPORT}"

record() { printf '%s\n' "$*" >> "${REPORT}"; }

# result, version, then the commit it was resolved at and a note, both optional. A line with no
# commit takes the short shape, so it does not end in the blanks of an empty column.
outcome() {
    local commit="${3-}" note="${4-}"
    if [[ -n ${commit} ]]; then
        printf '  %-7s %-17s %s\n' "$1" "$2" "${commit}${note:+   ${note}}" >> "${REPORT}"
    else
        printf '  %-7s %s\n' "$1" "$2" >> "${REPORT}"
    fi
}

# Resolve, and record the outcome. $2, when given, is the commit HEAD sits at.
resolve() {
    if ${MVN_CMD}; then
        outcome "ok" "$1" "${2-}"
    else
        outcome "FAILED" "$1" "${2-}"
    fi
}

: > "${REPORT}"
record "Jahia Maven cache warmup"

# $MVN_CMD is a full command line, so it is deliberately left unquoted at every call site.
git clone git@github.com:Jahia/jahia-private.git
cd jahia-private
record "jahia-private, default branch $(git rev-parse --abbrev-ref HEAD)"
record ""
record "Reactor resolutions:"

echo "Extracting version from POM"
DEFAULT_VERSION=$(mvn --batch-mode --quiet --settings ../maven.settings.xml help:evaluate -Dexpression=project.version -DforceStdout)

echo "Resolving dependencies for the default version (${DEFAULT_VERSION})"
${MVN_CMD}
outcome "ok" "${DEFAULT_VERSION}" "$(git rev-parse --short HEAD)" "(default branch)"

for version in ${JAHIA_VERSIONS}; do
    tag="JAHIA_${version//./_}"
    echo "Checking out and resolving dependencies for ${tag}"
    # Detached, so a repeated version fails on the tag rather than on a duplicate branch name.
    if git checkout --detach "${tag}"; then
        resolve "${version}" "$(git rev-parse --short HEAD)"
    else
        outcome "NO TAG" "${version}"
    fi
done
record ""
record "Parent chain resolutions:"

# The default version is warmed through the parent chain as well.
JAHIA_VERSIONS="${DEFAULT_VERSION} ${JAHIA_VERSIONS}"

cd ..
rm -rf jahia-private

echo "Create a dummy Maven project, with org.jahia.modules:jahia-modules as parent, for each version of JAHIA_VERSIONS"
mkdir -p dummy-project
cd dummy-project

for version in ${JAHIA_VERSIONS}; do
    echo "Creating POM for Jahia version $version"
    cat > pom.xml <<POM
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.jahia.modules</groupId>
        <artifactId>jahia-modules</artifactId>
        <version>$version</version>
    </parent>
    <groupId>com.example</groupId>
    <artifactId>dummy-project</artifactId>
    <version>1.0.0</version>
</project>
POM
    # Keep one warmup project pom reused by both OWASP and Sonar warmup blocks.
    if [[ ! -f ${SAST_WARMUP_POM} ]]; then
        mkdir -p ${SAST_WARMUP_DIR}
        cp pom.xml ${SAST_WARMUP_POM}
    fi
    cat pom.xml
    echo "Resolving dependencies for Jahia version $version"
    resolve "${version}"
done

cd ..
rm -rf dummy-project

echo "Remove SNAPSHOT dependencies from the local Maven repository as, by nature, they are supposed to change"
find ~/.m2/repository -name "*-SNAPSHOT" -type d -exec rm -rf {} + 2>/dev/null || true
record ""
record "SNAPSHOT artifacts were removed after the resolutions above."
