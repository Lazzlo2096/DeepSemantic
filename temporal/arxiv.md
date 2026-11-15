# arXiv Grabber

## Запуск через API

Из корня проекта запустите `just up`: скачивание, извлечение Markdown, суммаризация и индексация автоматически запускаются внутри `temporal-orchestrator`. Веткой саммари управляйте на странице «Воркеры» или через `PUT /pipeline/settings`. Для обновления миграций, API и обработки выполните `docker compose up -d --build migrate api temporal-orchestrator`.

Перед применением миграций `0019_drop_arxiv_import_result` или `0027_arxiv_import_attempts` дождитесь окончания текущего импорта и выполните `docker compose stop api temporal-orchestrator`. Затем запустите миграции и новые версии сервисов командой выше.

```powershell
$body = @{
    date_from = '2023-06-05'
    date_to = '2023-06-06'
    query = 'cat:cs.AI'
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/arxiv/imports' `
    -ContentType 'application/json' -Body $body
```

`POST /arxiv/imports` принимает даты, необязательные `bucket`, `query` и `request_id`.
API сохраняет параметры запроса и запускает Temporal напрямую. 202 означает,
что Temporal подтвердил запуск; при его недоступности возвращается 503.
Повторяйте запрос с тем же `request_id`, чтобы не создать второй импорт.

Пауза и продолжение — сигналы workflow. Статусы, попытки и прогресс читаются
из Temporal; в PostgreSQL этих полей нет. Манифест статей хранится в RustFS.
После скачивания статьи запускается извлечение Markdown, затем параллельно
индексация статьи и суммаризация с индексацией саммари.

`ARXIV_SOURCE_FORMAT=html` включает обработку текста; PDF сохраняется как исходник.
[Миграция и подробности хранения](README.md).

## Каталог в pgAdmin

Каталог также доступен через API. `GET /papers?limit=20&offset=0` возвращает последние загрузки, `limit` принимает значения от 1 до 100. `GET /papers/{arxiv_id}` возвращает запись указанной версии статьи. Например, `GET /papers/2611.07253v1`. [Swagger UI](http://127.0.0.1:8000/docs#/papers)

Откройте **DeepSemantic PostgreSQL → Databases → deepsemantic → Schemas → public → Tables → arxiv_papers**. Если pgAdmin уже открыт, обновите узел **Tables**. [pgAdmin](http://127.0.0.1:5050)

```sql
SELECT arxiv_id, arxiv_import_id, title, submitted_at, bucket, object_key, text_object_key, downloaded_at
FROM public.arxiv_papers
ORDER BY downloaded_at DESC, arxiv_id;
```

`arxiv_import_id` ссылается на задание, при котором текущая копия исходника попала в каталог. Повторное использование той же копии сохраняет эту связь. Для старых записей и копий, добавленных через CLI, поле равно `NULL`.

Строка появляется после подтверждения, что исходник есть в RustFS. `arxiv_id` включает версию статьи и является первичным ключом. `submitted_at` — дата и время первой подачи, `downloaded_at` — время загрузки объекта в бакет по данным RustFS. `object_key` указывает на PDF или HTML. `text_object_key` указывает на TXT или Markdown в том же бакете и заполняется после сохранения извлечённого текста. При импорте той же версии в другой бакет каталог указывает на копию с более свежим `downloaded_at`. При равенстве времени выбирается бакет, который идёт первым по имени. Поля счётчиков API `pdf_available`, `pdf_reused` и `pdf_failures` сохраняют имена и отражают состояние исходников.

Запросы к arXiv выполняются последовательно с интервалом не менее трёх секунд. Для массовой загрузки архивов arXiv предоставляет [доступ к PDF через S3](https://info.arxiv.org/help/bulk_data_s3.html). [Руководство по API arXiv](https://info.arxiv.org/help/api/user-manual.html) · [Условия использования API](https://info.arxiv.org/help/api/tou.html).
