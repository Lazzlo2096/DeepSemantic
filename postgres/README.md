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

## Миграции

Ревизии Alembic лежат в `postgres/migrations/versions/`. При запуске через `just up` сервис `migrate` выполняет `alembic upgrade head` после готовности PostgreSQL. API запускается после успешного завершения миграций. Первые две ревизии поддерживают и пустую базу, и таблицу, которую ранее создавал API.

Проверить текущую ревизию можно командой `docker compose run --rm migrate current`.

## Документы

В таблице `documents` хранятся `id`, исходный `text` и время создания `created_at`.

Поля `qdrant_collection` (`text`) и `qdrant_point_id` (`bigint`) предназначены для ссылки на точку в Qdrant. Пока ссылка не записана, оба поля имеют значение `NULL`.

В [Swagger UI](http://127.0.0.1:8000/docs) отправьте `POST /documents` с телом `{"text":"Текст для индексации"}`. Успешный запрос возвращает `201 Created` с полями `id` и `created_at`. Пустой или пробельный текст возвращает `422`. Построение эмбеддинга выполняется отдельным вызовом.

- `GET /documents/{document_id}` возвращает `id`, `text`, `created_at`, `qdrant_collection` и `qdrant_point_id`. До индексации оба поля Qdrant возвращаются как `null`. Если документ отсутствует, API возвращает `404`.
- `GET /documents?limit=20&offset=0` возвращает массив документов с теми же полями в порядке возрастания `id`. `limit` принимает значения от 1 до 100, `offset` задаёт число пропускаемых записей. Пустая страница возвращает `[]`.
- `POST /documents/{document_id}/embedding` читает текст из PostgreSQL, строит эмбеддинг и записывает его в коллекцию `documents_bge_m3` с ID документа в качестве ID точки. После подтверждения записи в Qdrant сервис заполняет `qdrant_collection` и `qdrant_point_id` в PostgreSQL и возвращает JSON-массив из 1024 чисел. Повторный запрос обновляет ту же точку. Для отсутствующего документа возвращается `404`, при превышении лимитов модели — `422`.
