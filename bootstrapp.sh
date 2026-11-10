#!/usr/bin/env bash
set -euo pipefail

if (( EUID != 0 )); then
    printf 'Run this script with sudo: sudo bash %s\n' "$0" >&2
    exit 1
fi

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

bash ./create-swapfile.sh

if [[ ! -e .env && ! -L .env ]]; then
    cp --update=none --no-target-directory .env.example .env
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y just docker.io docker-compose-v2 docker-buildx
systemctl enable --now docker.service

exec just up-server
