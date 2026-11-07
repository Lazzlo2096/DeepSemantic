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
    docker compose -f compose.yaml -f compose.amd.yaml run --rm --no-deps --entrypoint ollama ollama-init list
    docker compose stop ollama embeding-service
    docker compose -f compose.yaml -f compose.amd.yaml up --build --detach

down:
    docker compose down

logs:
    docker compose logs --follow

ps:
    docker compose ps
