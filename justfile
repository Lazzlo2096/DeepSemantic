set windows-shell := ["powershell.exe", "-NoProfile", "-Command"]
set dotenv-load := true

embedding_provider := env_var_or_default("EMBEDDING_PROVIDER", "openrouter")

default:
    @just --list

build:
    docker compose --profile {{embedding_provider}} build

up *compose_args:
    docker compose --profile {{embedding_provider}} {{compose_args}} up --build --detach --remove-orphans

up-server: (up "-f" "compose.yaml" "-f" "compose.server.yaml")

restart *compose_args:
    docker compose --profile {{embedding_provider}} {{compose_args}} up --build --detach --remove-orphans --no-deps --force-recreate --wait auth-service api fontend

stop:
    docker compose --profile langsmith-studio --profile tg-bot --profile deepkaravan stop

down:
    docker compose --profile langsmith-studio --profile tg-bot --profile deepkaravan down

# Telegram bot (polling).
tg-bot-up:
    docker compose --profile tg-bot up --build --detach tg-bot

tg-bot-down:
    docker compose --profile tg-bot stop tg-bot

tg-bot-logs:
    docker compose --profile tg-bot logs --follow tg-bot

# Local Agent Server for LangSmith Studio (https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024).
langsmith-studio-up:
    docker compose --profile {{embedding_provider}} --profile langsmith-studio up --build --detach --wait langsmith-studio

langsmith-studio-down:
    docker compose --profile langsmith-studio stop langsmith-studio

langsmith-studio-logs:
    docker compose --profile langsmith-studio logs --follow langsmith-studio

logs:
    docker compose --profile tg-bot --profile deepkaravan logs --follow

ps:
    docker compose --profile langsmith-studio --profile tg-bot --profile deepkaravan ps --all

temporal-test:
    python tests/temporal/run.py

# Ubuntu/server: stop services and save PostgreSQL + persistent volumes.
backup directory="":
    python3 scripts/migrate.py backup "{{directory}}"

# Restore only into empty volumes; services remain stopped.
restore directory:
    python3 scripts/migrate.py restore "{{directory}}"
