# Building the test stack without Docker Hub

`docker-compose.test.yml` uses `python:3.13-slim` and `postgres:16-alpine` by default. Where Docker Hub is blocked
(as in the environment this was developed in), build equivalent images from an Ubuntu mirror instead:

```bash
sudo debootstrap --variant=minbase noble rootfs http://archive.ubuntu.com/ubuntu
sudo tar -C rootfs -c . | docker import - qa-base:noble
docker build --network host -t qa-local/python:3.12 -f docker/local/Dockerfile.python docker/local
docker build --network host -t qa-local/postgres:16 -f docker/local/Dockerfile.postgres docker/local
docker build --network host --build-arg PYTHON_IMAGE=qa-local/python:3.12 \
  --secret id=pip_ca,src=/path/to/proxy-ca.crt \
  -t s2t-qa-tests:latest -f Dockerfile.test .   # --secret only behind a TLS-intercepting proxy
TEST_DB_IMAGE=qa-local/postgres:16 NO_BUILD=1 scripts/run-tests-docker.sh
```
