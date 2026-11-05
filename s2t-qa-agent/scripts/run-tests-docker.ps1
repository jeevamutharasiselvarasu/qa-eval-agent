# Windows (Docker Desktop): run the backend test suite against a separate, throwaway PostgreSQL container.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
New-Item -ItemType Directory -Force -Path test-results | Out-Null
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from tests
$code = $LASTEXITCODE
docker compose -f docker-compose.test.yml down -v --remove-orphans | Out-Null
Write-Host "Test exit code: $code (JUnit report: test-results/junit.xml)"
exit $code
