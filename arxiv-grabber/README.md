# arXiv Grabber

## Запуск через API

Из корня проекта запустите `just up`, затем включите скачивание, извлечение текста и суммаризацию на странице «Воркеры». Чтобы обновить миграции и API и сразу запустить воркер, выполните `docker compose up -d --build migrate api arxiv-grabber arxiv-text-worker summary-worker`.

Перед применением миграций `0019_drop_arxiv_import_result` или `0027_arxiv_import_attempts` дождитесь окончания текущего импорта и выполните `docker compose stop api arxiv-grabber`. Затем запустите миграции и новые версии сервисов командой выше.

```powershell
$body = @{
    date_from = '2023-06-05'
    date_to = '2023-06-06'
    query = 'cat:cs.AI'
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/arxiv/imports' `
    -ContentType 'application/json' -Body $body
```

`POST /arxiv/imports` принимает даты в формате `YYYY-MM-DD`, необязательные `bucket` и `query`. API возвращает `202 Accepted` и `{"id":"…"}` после записи задания в PostgreSQL `arxiv_imports`. При неверных параметрах API возвращает `422`, при недоступности PostgreSQL — `503`.

Фоновое исполнение управляется Temporal: отдельные Activities обнаруживают список
статей и скачивают каждую статью. Список и прогресс сохраняются в PostgreSQL;
при перезапуске завершённые этапы не запускаются заново. `completed` означает,
что скачивание завершено; извлечение текста, суммаризация и индексация выполняются
независимыми заданиями. Подробности ошибок видны в Temporal UI и `temporal_jobs`.

«Приостановить» останавливает импорт после текущей операции. «Продолжить» использует
сохранённый список статей. Остановка контейнера сохраняет задания в Temporal.

`ARXIV_SOURCE_FORMAT=html` выбирает HTML; `pdf` оставлен для скачивания PDF без
автоматического извлечения текста. Markdown сохраняется в RustFS по ключу с хешем
содержимого. Саммари публикуется только для актуальной версии Markdown.

[Инструкция по обновлению, очереди, повторы и резервное копирование](../temporal/README.md).
Старые воркеры необходимо остановить до миграции `0038`.

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
