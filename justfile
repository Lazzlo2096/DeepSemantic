set windows-shell := ["powershell.exe", "-NoProfile", "-Command"]

default:
    @just --list

build:
    docker compose build

up:
    docker compose up --build --detach

up-gpu:
    docker compose -f compose.yaml -f compose.cuda.yaml up --build --detach

up-amd:
    docker compose -f compose.yaml -f compose.amd.yaml run --rm --no-deps --entrypoint ollama ollama-init list # Выводит список доступных моделей. Just не продолжит если ничего не найдёт
    docker compose stop ollama embeding-service # Ещё раз останавливаем, на всякий случай, чтобы можно было после just up запустить just-up-amd
    docker compose -f compose.yaml -f compose.amd.yaml up --build --detach

down:
    docker compose down

logs:
    docker compose logs --follow

ps:
    docker compose ps
