#!/usr/bin/env bash
set -euo pipefail

if (( EUID != 0 )); then
    printf 'Run this script with sudo: sudo bash %s\n' "$0" >&2
    exit 1
fi

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

printf '[bootstrap] Starting setup in %s.\n' "$PWD"
printf '[bootstrap] Checking swap configuration.\n'
bash ./create-swapfile.sh

if [[ ! -e .env && ! -L .env ]]; then
    printf '[bootstrap] Creating .env from .env.example.\n'
    cp --update=none --no-target-directory .env.example .env
else
    printf '[bootstrap] Using the existing .env.\n'
fi

printf '[bootstrap] Updating APT package lists.\n'
apt-get update
printf '[bootstrap] Installing just, Docker Engine, Docker Compose v2 and Buildx.\n'
DEBIAN_FRONTEND=noninteractive apt-get install -y just docker.io docker-compose-v2 docker-buildx
printf '[bootstrap] Starting Docker and enabling it at boot.\n'
systemctl enable --now docker.service

printf '[bootstrap] Building and starting the server with just up-server.\n'
exec just up-server
