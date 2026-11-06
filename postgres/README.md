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

## Документы

API создаёт таблицу `documents` при запуске, в том числе при подключении к существующему тому. В таблице хранятся `id`, исходный `text` и время создания `created_at`.

В [Swagger UI](http://127.0.0.1:8000/docs) отправьте `POST /documents` с телом `{"text":"Текст для индексации"}`. Успешный запрос возвращает `201 Created` с полями `id` и `created_at`. Пустой или пробельный текст возвращает `422`. Построение эмбеддинга выполняется отдельным вызовом.

- `GET /documents/{document_id}` возвращает `id`, `text` и `created_at`. Если документ отсутствует, API возвращает `404`.
- `GET /documents?limit=20&offset=0` возвращает массив документов в порядке возрастания `id`. `limit` принимает значения от 1 до 100, `offset` задаёт число пропускаемых записей. Пустая страница возвращает `[]`.
