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
DEBIAN_FRONTEND=noninteractive apt-get install -y just docker.io docker-compose-v2 docker-buildx python3
printf '[bootstrap] Starting Docker and enabling it at boot.\n'
systemctl enable --now docker.service

cat <<'EOF'

[bootstrap] Установка завершена!

Откройте .env и укажите ключ OpenRouter в OPENROUTER_API_KEY.
Выберите модели для поиска по RAG (RAG_OPENROUTER_MODEL) и суммаризации (SUMMARY_OPENROUTER_MODEL).

Запустите проект командой just up-server.
Для остановки выполните just down.

Пользуйтесь! :-)
EOF
