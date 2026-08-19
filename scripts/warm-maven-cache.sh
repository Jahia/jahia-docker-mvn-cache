#!/usr/bin/env bash
# Warm /root/.m2 with the artifacts a Jahia CI build asks for.
#
# Two lists, two very different costs:
#
#   JAHIA_CORE_VERSIONS           tags checked out in jahia-private and resolved as a full
#                                 reactor. Minutes each, and casts a wide net over the
#                                 product's third-party dependencies.
#   JAHIA_MODULE_PARENT_VERSIONS  versions resolved through a throwaway pom whose parent is
#                                 org.jahia.modules:jahia-modules. Cheap, and it is the exact
#                                 chain a module build walks: the parent pom, its pinned
#                                 plugins, and jahia-impl / jahia-taglib at that version.
#
# Both lists come in as build args. See the README for how they are picked.
#
# A version that cannot be resolved is reported and skipped: one unresolvable version must not
# leave every Jahia repository without an image.

set -euo pipefail

# The image has no WORKDIR, so this runs at /. Strip the trailing slash to compose plain paths.
WORK_DIR="$(pwd)"
SETTINGS="${WORK_DIR%/}/maven.settings.xml"
CLONE_DIR="${WORK_DIR%/}/jahia-private"
DUMMY_DIR="${WORK_DIR%/}/dummy-project"

FAILURES=()

# $COMMON_GOALS is a space-separated goal list, so it is deliberately left unquoted here.
mvn_resolve() {
    mvn -B -s "${SETTINGS}" dependency:resolve dependency:resolve-plugins ${COMMON_GOALS}
}

# Resolve, but keep going when a version is unresolvable.
try_resolve() {
    local label="$1"
    if mvn_resolve; then
        echo "Resolved ${label}"
    else
        echo "::warning title=Maven cache warmup::Could not resolve ${label}; builds on that version will fetch from the remote repository instead of the cache"
        FAILURES+=("${label}")
    fi
}

echo "=== Resolving the jahia-private reactor ==="
git clone git@github.com:Jahia/jahia-private.git "${CLONE_DIR}"
cd "${CLONE_DIR}"

# The version on the default branch. Its dependencies are the ones every current build needs,
# so a failure here means the image is not worth publishing.
DEFAULT_VERSION="$(mvn -B -s "${SETTINGS}" help:evaluate -Dexpression=project.version -q -DforceStdout)"
echo "Resolving the default version (${DEFAULT_VERSION})"
mvn_resolve

for version in ${JAHIA_CORE_VERSIONS}; do
    tag="JAHIA_${version//./_}"
    echo "Checking out ${tag}"
    if git checkout -b "${tag}" "${tag}"; then
        try_resolve "jahia-private ${version}"
    else
        echo "::warning title=Maven cache warmup::Tag ${tag} not found in jahia-private"
        FAILURES+=("jahia-private ${version} (no such tag)")
    fi
done

cd "${WORK_DIR}"
rm -rf "${CLONE_DIR}"

# The union of both lists, first occurrence wins, so a version named twice is resolved once.
PARENT_VERSIONS="$(printf '%s\n' ${DEFAULT_VERSION} ${JAHIA_CORE_VERSIONS} ${JAHIA_MODULE_PARENT_VERSIONS} \
    | awk 'NF && !seen[$0]++')"

echo "=== Resolving the jahia-modules parent chain ==="
mkdir -p "${DUMMY_DIR}"
cd "${DUMMY_DIR}"

for version in ${PARENT_VERSIONS}; do
    echo "Building a pom against org.jahia.modules:jahia-modules:${version}"
    cat > pom.xml <<POM
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.jahia.modules</groupId>
        <artifactId>jahia-modules</artifactId>
        <version>${version}</version>
    </parent>
    <groupId>com.example</groupId>
    <artifactId>dummy-project</artifactId>
    <version>1.0.0</version>
</project>
POM
    # One of these poms is kept for the OWASP and Sonar warmup steps that follow.
    if [[ ! -f ${SAST_WARMUP_POM} ]]; then
        mkdir -p "${SAST_WARMUP_DIR}"
        cp pom.xml "${SAST_WARMUP_POM}"
    fi
    try_resolve "jahia-modules parent ${version}"
done

cd "${WORK_DIR}"
rm -rf "${DUMMY_DIR}"

echo "Removing SNAPSHOT artifacts, which are expected to change"
find ~/.m2/repository -name "*-SNAPSHOT" -type d -exec rm -rf {} + 2>/dev/null || true

echo "=== Maven cache warmup summary ==="
if ((${#FAILURES[@]})); then
    echo "${#FAILURES[@]} version(s) were not warmed:"
    printf '  - %s\n' "${FAILURES[@]}"
else
    echo "Every requested version was warmed."
fi
