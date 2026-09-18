#!/bin/sh
set -eu

source_dir=/etc/grafana/provisioning
runtime_dir=/tmp/sahabino-grafana-provisioning

rm -rf "$runtime_dir"
mkdir -p "$runtime_dir"
cp -R "$source_dir"/. "$runtime_dir"/

if [ -z "${GRAFANA_POSTGRES_PASSWORD:-}" ]; then
    rm -f "$runtime_dir/datasources/postgres.yaml"
fi

export GF_PATHS_PROVISIONING="$runtime_dir"
exec /run.sh "$@"
