# PostgreSQL

Запуск из корня проекта через `docker compose up -d postgres` или `just up`. Данные сохраняются в томе `postgres-data`.

Контейнеры Compose подключаются по адресу `postgres:5432`. Имя базы, пользователь и пароль для локального запуска по умолчанию — `deepsemantic`. Перед первой инициализацией их можно задать через `POSTGRES_DB`, `POSTGRES_USER` и `POSTGRES_PASSWORD`.

Открыть `psql` с настройками контейнера можно командой:

```sh
docker compose exec postgres sh -c 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

## pgAdmin

Запустите `docker compose up -d pgadmin` и откройте http://127.0.0.1:5050. Для первого входа по умолчанию используются `admin@deepsemantic.dev` и пароль `deepsemantic`. Их можно задать через `PGADMIN_DEFAULT_EMAIL` и `PGADMIN_DEFAULT_PASSWORD` перед инициализацией тома `pgadmin-data`.

При первой инициализации pgAdmin импортирует **DeepSemantic PostgreSQL** из `pgadmin/servers.json`. При подключении введите пароль PostgreSQL. Если имя базы или пользователя отличается от значения по умолчанию, укажите его в настройках подключения.

В уже инициализированном pgAdmin файл можно загрузить через **Tools → Import/Export Servers**, сохранив существующие подключения.
