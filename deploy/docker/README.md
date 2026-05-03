# Lumen Docker Deployment

This directory holds Docker-only deployment helpers that sit next to the main
`docker-compose.yml`.

## Local Compose Override

Use `docker-compose.local.yml` for isolated build or smoke tests on a host that
already has a production Lumen stack. The main compose file uses fixed
`container_name` values (`lumen-api`, `lumen-pg`, ...), so a second stack on the
same machine needs a different project, ports, and data root:

```bash
mkdir -p /tmp/lumen-local/{postgres,redis,storage,backup}
chown -R 70:70 /tmp/lumen-local/postgres
chown -R 999:999 /tmp/lumen-local/redis
chown -R 10001:10001 /tmp/lumen-local/storage /tmp/lumen-local/backup

cp .env.example .env.local
LUMEN_DATA_ROOT=/tmp/lumen-local \
LUMEN_IMAGE_TAG=local \
COMPOSE_PROJECT_NAME=lumen-local \
docker compose --env-file .env.local -f docker-compose.yml -f deploy/docker/docker-compose.local.yml config
```

The production path should continue to use `scripts/install.sh`,
`scripts/update.sh`, and `scripts/lumenctl.sh`; this file is only an operator
helper for local validation.
