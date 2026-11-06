set windows-shell := ["powershell.exe", "-NoProfile", "-Command"]

default:
    @just --list

build:
    docker compose build

up:
    docker compose up --build --detach

down:
    docker compose down

logs:
    docker compose logs --follow

ps:
    docker compose ps
