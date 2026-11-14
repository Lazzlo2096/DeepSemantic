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
    docker compose --profile workers --profile langsmith-studio down

# Local Agent Server for LangSmith Studio (https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024).
langsmith-studio-up:
    docker compose --profile langsmith-studio up --build --detach --wait langsmith-studio

langsmith-studio-down:
    docker compose --profile langsmith-studio stop langsmith-studio

langsmith-studio-logs:
    docker compose --profile langsmith-studio logs --follow langsmith-studio

logs:
    docker compose --profile workers logs --follow

ps:
    docker compose --profile workers --profile langsmith-studio ps --all

# Ubuntu/server: stop services and save PostgreSQL + persistent volumes.
backup directory="":
    python3 scripts/migrate.py backup "{{directory}}"

# Restore only into empty volumes; services remain stopped.
restore directory:
    python3 scripts/migrate.py restore "{{directory}}"
