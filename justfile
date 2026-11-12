set windows-shell := ["powershell.exe", "-NoProfile", "-Command"]

workers := "arxiv-grabber arxiv-text-worker embeding-worker summary-worker"

default:
    @just --list

build:
    docker compose --profile workers build

up *compose_args:
    docker compose {{compose_args}} up --build --detach --remove-orphans
    docker compose {{compose_args}} up --build --no-start --no-deps {{workers}}

up-server: (up "-f" "compose.yaml" "-f" "compose.server.yaml")

restart *compose_args:
    docker compose {{compose_args}} up --build --detach --remove-orphans --no-deps --force-recreate --wait api fontend

down:
    docker compose --profile workers down

logs:
    docker compose --profile workers logs --follow

ps:
    docker compose --profile workers ps --all

# Ubuntu/server: stop services and save PostgreSQL + persistent volumes.
backup directory="":
    python3 scripts/migrate.py backup "{{directory}}"

# Restore only into empty volumes; services remain stopped.
restore directory:
    python3 scripts/migrate.py restore "{{directory}}"
