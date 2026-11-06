# PostgreSQL

Запуск из корня проекта через `docker compose up -d postgres` или `just up`. Данные сохраняются в томе `postgres-data`.

Контейнеры Compose подключаются по адресу `postgres:5432`. Имя базы, пользователь и пароль для локального запуска по умолчанию — `deepsemantic`. Перед первой инициализацией их можно задать через `POSTGRES_DB`, `POSTGRES_USER` и `POSTGRES_PASSWORD`.

Открыть `psql` с настройками контейнера можно командой:

```sh
docker compose exec postgres sh -c 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

## pgAdmin

Запустите `docker compose up -d pgadmin` и откройте http://127.0.0.1:5050. Для первого входа по умолчанию используются `admin@deepsemantic.dev` и пароль `deepsemantic`. Их можно задать через `PGADMIN_DEFAULT_EMAIL` и `PGADMIN_DEFAULT_PASSWORD` перед инициализацией тома `pgadmin-data`.

В pgAdmin выберите **Register → Server**. На вкладке **Connection** укажите хост `postgres`, порт `5432`, базу `deepsemantic` и учётные данные PostgreSQL.
