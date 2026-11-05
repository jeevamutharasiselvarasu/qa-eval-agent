#!/bin/bash
set -euo pipefail
: "${POSTGRES_USER:=postgres}" "${POSTGRES_DB:=$POSTGRES_USER}" "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
BIN=/usr/lib/postgresql/16/bin
mkdir -p "$PGDATA" /var/run/postgresql
chown -R postgres:postgres "$PGDATA" /var/run/postgresql
chmod 700 "$PGDATA"
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  pwfile=$(mktemp); echo "$POSTGRES_PASSWORD" > "$pwfile"; chown postgres "$pwfile"
  su postgres -c "$BIN/initdb -D '$PGDATA' -U '$POSTGRES_USER' --pwfile='$pwfile' --auth-host=scram-sha-256 --auth-local=trust --encoding=UTF8 --locale=C.UTF-8" >/dev/null
  rm -f "$pwfile"
  echo "host all all 0.0.0.0/0 scram-sha-256" >> "$PGDATA/pg_hba.conf"
  su postgres -c "$BIN/pg_ctl -D '$PGDATA' -o '-c listen_addresses=' -w start" >/dev/null
  su postgres -c "$BIN/psql -U '$POSTGRES_USER' -d postgres -c 'CREATE DATABASE \"$POSTGRES_DB\"'" >/dev/null
  su postgres -c "$BIN/pg_ctl -D '$PGDATA' -m fast -w stop" >/dev/null
fi
exec su postgres -c "$BIN/postgres -D '$PGDATA' -c listen_addresses='*'"
