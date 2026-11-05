#!/usr/bin/env bash
# Run the backend test suite in Docker against a separate, throwaway PostgreSQL container.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p test-results && chmod 777 test-results
COMPOSE="docker compose -f docker-compose.test.yml"
BUILD_FLAG="--build"
[ "${NO_BUILD:-0}" = "1" ] && BUILD_FLAG="--no-build"
set +e
$COMPOSE up $BUILD_FLAG --abort-on-container-exit --exit-code-from tests
code=$?
set -e
$COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true
echo "Test exit code: $code (JUnit report: test-results/junit.xml)"
exit $code
