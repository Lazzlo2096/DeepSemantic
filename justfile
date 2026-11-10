set windows-shell := ["powershell.exe", "-NoProfile", "-Command"]

workers := "arxiv-grabber arxiv-text-worker embeding-worker summary-worker"
container_workers := workers + " embeding-service"

default:
    @just --list

build:
    docker compose --profile workers build

up *compose_args:
    docker compose {{compose_args}} up --build --detach
    docker compose {{compose_args}} up --build --no-start --no-deps {{container_workers}}

up-server: (up "-f" "compose.yaml" "-f" "compose.server.yaml")

up-gpu:
    docker compose -f compose.yaml -f compose.cuda.yaml up --build --detach
    docker compose -f compose.yaml -f compose.cuda.yaml up --build --no-start --no-deps {{container_workers}}

up-amd:
    docker compose -f compose.yaml -f compose.amd.yaml run --rm --no-deps --entrypoint ollama ollama-init list # Выводит список доступных моделей. Just не продолжит если ничего не найдёт
    docker compose stop ollama embeding-service # Ещё раз останавливаем, на всякий случай, чтобы можно было после just up запустить just-up-amd
    docker compose -f compose.yaml -f compose.amd.yaml up --build --detach
    docker compose -f compose.yaml -f compose.amd.yaml up --build --no-start --no-deps {{workers}}

restart *compose_args:
    docker compose {{compose_args}} up --build --detach --no-deps --force-recreate --wait api fontend

down:
    docker compose --profile workers down

logs:
    docker compose --profile workers logs --follow

ps:
    docker compose --profile workers ps --all
