# DeepSemantic

## Фоновые процессы на Temporal

Импорт, извлечение Markdown, суммаризация и индексация выполняются цепочками
Temporal. API запускает workflow напрямую; статусы, пауза, попытки, прогресс
и ошибки находятся в Temporal. PostgreSQL хранит параметры импортов и готовые
данные: статьи, саммари и чанки. SQL-диспетчера и таблиц очередей нет.
[Схема хранения, миграция и запуск](temporal/README.md).
[Temporal UI](http://127.0.0.1:8088).

## RAG на LangChain и LangGraph

`POST /rag/answer` выполняет граф из `api/rag.py`. LangChain использует
`ChatOpenRouter` для вызовов модели и `StructuredTool` для инструментов поиска
и чтения. LangGraph управляет состоянием запроса и переходами:

```text
Начало → research
research → tools → research                         (есть вызовы инструментов)
research → answer → Конец                           (поиск завершён, есть источники)
research → insufficient_evidence → Конец            (поиск завершён, источников нет)
```

На этапе `research` модель получает `RESEARCH_INSTRUCTIONS`, вопрос и накопленную
историю поиска. Доступны `search_vectors`, `search_text` и `read_document`.
Инструменты выполняются последовательно, чтобы сохранялся порядок ID источников
и шагов в интерфейсе. Векторный поиск обращается к индексам статей и заранее
созданных суммаризаций в Qdrant; буквальный поиск выполняется в PostgreSQL.

После завершения поиска узел `answer` делает отдельный вызов с
`ANSWER_INSTRUCTIONS`, исходным вопросом, шагами поиска и собранными источниками.
Ответ запрашивается через `with_structured_output` в формате JSON Schema;
API дополнительно проверяет полноту JSON и ссылки на существующие источники.
Если источников нет, вызов генерации пропускается. Граф ограничен 1000 шагами;
при достижении лимита запуск завершается ошибкой.

Модель, reasoning и порядок провайдеров задаются прежними переменными
`RAG_OPENROUTER_MODEL`, `RAG_OPENROUTER_REASONING_EFFORT` и
`OPENROUTER_PROVIDER_ORDER`. При пустом reasoning отправляется `effort: "none"`.
История модели и инструментов сохраняется в прежних таблицах; доступны прежние
ответы API и `GET /rag/runs/{run_id}`. История служит журналом, автоматического
возобновления графа с сохранённого шага нет. Воркер суммаризации статей работает
отдельно от RAG.

Проверка без ключей OpenRouter и работающих хранилищ (Python 3.11+):

```sh
python -m pip install -r api/requirements.txt -r temporal/requirements.txt
python -m unittest discover -s tests -v
```

## Пользователи и авторизация

Caddy проверяет защищённые запросы через отдельный `auth-service`. Каталог и готовые материалы доступны без входа. RAG и векторный поиск требуют аккаунт, а импорт, суммаризация и настройки обработки — роль администратора. Сессии и приглашения хранятся в схеме PostgreSQL `auth`; история RAG привязана к создавшему её пользователю.

Для локального запуска создайте первого администратора после миграций:

```sh
docker compose run --rm auth-service python -m auth_service.cli create-admin admin@example.com
```

На сервере сначала задайте случайный `AUTH_PROXY_SECRET` в `.env`. Подробности находятся в [описании auth-service](auth-service/README.md).

## LangSmith Studio

Запусти локальный Agent Server с графом RAG:

```sh
just langsmith-studio-up
# Или без just:
docker compose --profile langsmith-studio up --build --detach --wait langsmith-studio
```

Открой [Studio с подключением к DeepSemantic](https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024)
и выбери граф `rag`. Если браузер запрашивает доступ к локальной сети, разреши
его для Studio. В Graph mode введи вопрос в поле `question`, например:

```json
{"question": "Какие методы уменьшают расход памяти при обучении нейросетей?"}
```

На схеме видны `prepare`, `research`, `tools`, `answer` и
`insufficient_evidence`. `prepare` начинает новый вопрос с чистой историей поиска.
Остальные узлы и переходы общие с API. Можно ставить остановки перед узлами,
изучать сообщения и источники, затем продолжать выполнение.

Agent Server доступен на [localhost:2024](http://127.0.0.1:2024),
его API описан на [странице docs](http://127.0.0.1:2024/docs).
Порт задаётся через `LANGSMITH_STUDIO_PORT` в `.env`; при смене порта обнови `baseUrl`
в ссылке Studio. Схема доступна без ключа OpenRouter. Для выполнения вопросов
используются существующий `OPENROUTER_API_KEY`, настройки RAG и хранилища проекта.

Studio работает через `langgraph dev` внутри Docker: это сервер для разработки
и отладки, доступный только на `127.0.0.1`. Лицензионный ключ для этого режима
не требуется. Трассировка в облачный LangSmith и аналитика CLI отключены.
История и checkpoints Studio сохраняются локально в volume `langsmith-studio-data`;
они сохраняются при обычном перезапуске контейнера. Сайт продолжает вести
свой журнал `/rag/runs`: запуски с сайта и запуски из Studio — отдельные истории.
Ключи, HTTP-клиенты и соединения с БД не записываются в checkpoints.

Если Docker запущен на удалённом сервере, открой на своём компьютере SSH-туннель:

```sh
ssh -N -L 2024:127.0.0.1:2024 user@server
```

Затем используй ту же ссылку Studio с `baseUrl=http://127.0.0.1:2024`.

```sh
just langsmith-studio-logs  # журнал Agent Server
just langsmith-studio-down # остановить Studio, сохранив историю
```

Сервис `langsmith-studio` собирается отдельным `langsmith-studio/Dockerfile`.
Существующий Docker volume с историей сохраняется под прежним физическим именем.
Адаптер находится в `langsmith-studio/studio.py`, регистрация графа и CORS — в
`langsmith-studio/langgraph.json`. Для тестирования адаптера достаточно обычных зависимостей
API и команды `python -m unittest discover -s tests -v`.

## Развёртывание на Ubuntu

Выполни на сервере от имени `root`.

```sh
git clone --depth 1 https://github.com/Lazzlo2096/DeepSemantic.git
cd DeepSemantic

chmod +x ./bootstrap.sh
./bootstrap.sh
```

После установки укажи в `.env` ключ `OPENROUTER_API_KEY` для RAG, суммаризации и эмбеддингов и выбери модели `RAG_OPENROUTER_MODEL` и `SUMMARY_OPENROUTER_MODEL`. По умолчанию эмбеддинги запрашиваются у OpenRouter через `EMBEDDING_PROVIDER=openrouter` и модель `EMBEDDING_OPENROUTER_MODEL=nvidia/nemotron-3.5-embed-8b:free`. HTTP-таймаут задаёт `EMBEDDING_TIMEOUT` (по умолчанию 60 секунд; `0` отключает HTTP-таймаут). Лимиты Temporal Activities сохраняются: для `index_batch` — 10 минут. Запускай и останавливай проект из каталога `DeepSemantic`.

```dotenv
# OpenRouter (по умолчанию)
EMBEDDING_PROVIDER=openrouter
COMPOSE_PROFILES=${EMBEDDING_PROVIDER:-openrouter}
EMBEDDING_OPENROUTER_MODEL=nvidia/nemotron-3.5-embed-8b:free
EMBEDDING_BASE_URL=
```

Пустой `EMBEDDING_BASE_URL` автоматически выбирает `https://openrouter.ai/api/v1/`. После изменения env пересоздай API и оркестратор командой `just up` / `just up-server`; все очереди обработки запускаются внутри `temporal-orchestrator`. Общий интерфейс находится в `api/embedding_service.py`, настройки провайдеров — в `api/embeddings_config.py`; API, RAG и воркеры используют `state.embeddings.embed(...)` без выбора провайдера в прикладном коде. Индексация отправляется с `input_type=search_document`, поисковые запросы — с `input_type=search_query`. При смене embedding-модели проект создаёт отдельные Qdrant-коллекции, поэтому старые векторы BGE-M3 не смешиваются с 4096-мерными векторами Nemotron.

Строка `COMPOSE_PROFILES=${EMBEDDING_PROVIDER:-openrouter}` должна находиться в `.env` после `EMBEDDING_PROVIDER`; добавь её и в существующий `.env` при обновлении. Для `openrouter` локальный контейнер не запускается. Чтобы явно вернуться к локальному DeepKaravan, одновременно задай `EMBEDDING_PROVIDER=deepkaravan`, `COMPOSE_PROFILES=${EMBEDDING_PROVIDER:-openrouter}` и `EMBEDDING_OPENROUTER_MODEL=baai/bge-m3`: локальный сервис поддерживает только BGE-M3. Пустой `EMBEDDING_BASE_URL` тогда выберет `http://deepkaravan:8000/api/v1/`. Отключение профиля не останавливает уже работающий контейнер: при переходе на облако выполни `docker compose --profile deepkaravan stop deepkaravan` либо `just down` перед повторным запуском. Команды `just` передают профиль выбранного провайдера явно; `just ps`, `just logs` и `just down` учитывают DeepKaravan при любом выбранном провайдере.

Перед первым запуском локального провайдера выполни `git submodule update --init --recursive`. При активном профиле основной Compose собирает DeepKaravan из сабмодуля и запускает на CPU в общей сети без публикации дополнительного порта. Настройки очереди и инференса можно задать в `DeepKaravan/.env`; устройство в основном стеке закреплено как `cpu`. Модель загружается при первом запуске и сохраняется в томе `deepkaravan-model-cache`. DeepKaravan поддерживает только эмбеддинги; RAG и суммаризация продолжают обращаться к OpenRouter. При запуске API вне Docker задай `EMBEDDING_BASE_URL` с доступным адресом DeepKaravan.

```sh
just up-server # запустить проект
just down      # остановить проект
```

При обновлении существующего стека `just up` / `just up-server` удаляет контейнеры сервисов, исключённых из Compose. Данные в старых томах сохраняются. Все очереди Temporal обслуживает один `temporal-orchestrator`, запускаемый автоматически. Отдельные контейнеры `arxiv-grabber`, `arxiv-text-worker`, `embeding-worker` и `summary-worker` удалены; повторная обработка запускается через Temporal. Старые резервные копии восстанавливай на соответствующей им ревизии проекта, затем обновляй код.

## Устройство DeepKaravan: CPU или AMD WSL

Режим локального инференса выбирается в `.env` проекта DeepSemantic:

```dotenv
EMBEDDING_PROVIDER=deepkaravan
DEEPKARAVAN_INFERENCE_DEVICE=amd-wsl
```

Для CPU укажите `DEEPKARAVAN_INFERENCE_DEVICE=cpu`. Основной `compose.yaml`
автоматически наследует настройки устройства через `extends` из
`compose.deepkaravan.<устройство>.yaml`. Переменные `COMPOSE_FILE` и
`COMPOSE_PATH_SEPARATOR` не нужны. Шаблон `.env.example` по умолчанию использует CPU.
Переменная определяет и стадию сборки образа, и `INFERENCE_DEVICE` внутри
контейнера; override AMD WSL передаёт `/dev/dxg`, библиотеки `/usr/lib/wsl/lib`
и выделяет 1 ГБ shared memory. Сохраняется общий том кеша модели.

После изменения выполните `just up` для пересборки и пересоздания контейнера.
`just up-server` и обычный `docker compose up -d --build` также учитывают
выбранное устройство. Подключать файл устройства вручную не нужно.
Для AMD WSL нужны Docker Desktop с WSL2 и подходящий AMD-драйвер;
подробности — в [DeepKaravan/README.md](DeepKaravan/README.md#windows--docker-desktop--amd-wsl).
Автоматического переключения AMD → CPU нет.

## Перенос на другой сервер

На Ubuntu доступны `just backup` и `just restore`. Нужен Python 3, который устанавливает
`bootstrap.sh`. По умолчанию команды используют `compose.yaml` + `compose.server.yaml`.

На старом сервере, пока PostgreSQL ещё запущен:

```sh
just backup
# Или задать каталог явно (он не должен существовать):
# just backup /srv/backups/deepsemantic-2026-11-12
```

Команда останавливает приложение и воркеры, создаёт `postgres.dump` через `pg_dump -Fc`,
затем останавливает PostgreSQL и архивирует остальные существующие volumes: Qdrant,
RustFS (файлы статей), PostgreSQL Temporal, Grafana, Loki, pgAdmin, Alloy и сертификаты Caddy.
Сервисы остаются остановленными, чтобы данные не изменились после копирования.
При ошибке они также автоматически не запускаются; неполная копия не имеет `manifest.json`.
Если перенос откладывается, запусти `just up-server`; перед переносом создай новую копию.

Результат находится в `backups/<дата-время>/`: дамп, архивы, `.env` и манифест
с контрольными суммами SHA-256. **Копия содержит пароли и ключи из `.env`**;
храни её приватно и передавай через SSH. Каталог `backups/` исключён из Git.
Внешние bind mounts не архивируются: конфигурация приложения переносится вместе с репозиторием.

На новом сервере установи ту же ревизию проекта и зависимости через `bootstrap.sh`,
но пока не запускай `just up-server`. В каталоге проекта создай `mkdir -p backups`.
Перенеси каталог копии со старого сервера:

```sh
# Выполняется со старого сервера; подставь адрес, путь проекта и имя копии.
scp -r backups/20261112-120000 root@NEW_SERVER:/srv/DeepSemantic/backups/
```

Затем в каталоге проекта на новом сервере:

```sh
cp backups/20261112-120000/.env .env
chmod 600 .env
just restore backups/20261112-120000
just up-server
```

Восстановление проверяет контрольные суммы, архитектуру сервера, версии образов
в Compose и пустоту всех целевых хранилищ. Существующие данные оно не удаляет:
для переноса нужен новый проект с пустыми volumes. PostgreSQL временно запускается
без приложения и миграций, дамп загружается через `pg_restore`, затем PostgreSQL
останавливается. Только после успешного восстановления запускай приложение.
При ошибке восстановления могут остаться частично заполненные volumes: сначала
разберись с причиной; повторный запуск поверх них будет отклонён.

Это перенос между серверами с одинаковой архитектурой и теми же версиями сервисов,
без одновременного обновления БД.
Пользовательские роли и дополнительные базы PostgreSQL вне основной базы приложения
не включены в дамп. Проверяй доступность статей и поиск до переключения трафика,
затем перенеси DNS на новый сервер. Старый сервер и копию сохраняй до проверки.

Для локального Compose или другого набора override-файлов вызывай скрипт напрямую,
передавая каждый файл через `-f`, одинаково при копировании и восстановлении:

```sh
python3 scripts/migrate.py backup -f compose.yaml
python3 scripts/migrate.py restore backups/20261112-120000 -f compose.yaml
```
