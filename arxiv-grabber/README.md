# arXiv Grabber

## Запуск через API

Из корня проекта запустите `just up` или обновите API и воркер в уже работающем стеке командой `docker compose up -d --build api arxiv-grabber`.

```powershell
$body = @{
    date_from = '2023-06-05'
    date_to = '2023-06-06'
    query = 'cat:cs.AI'
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/arxiv/imports' `
    -ContentType 'application/json' -Body $body
```

`POST /arxiv/imports` принимает даты в формате `YYYY-MM-DD`, необязательные `bucket` и `query`. API возвращает `202 Accepted` и `{"id":"…"}` после подтверждения публикации RabbitMQ. При неверных параметрах API возвращает `422`, при сбое подтверждения публикации — `503`.

Воркер выполняет задания из очереди `arxiv.imports` по одному. По ID из ответа можно найти начало и итог обработки в `docker compose logs arxiv-grabber`. Воркер подтверждает успешный импорт после записи результата. При обрыве соединения RabbitMQ возвращает неподтверждённое задание в очередь. Если импорт завершился с ошибкой, воркер пишет её в лог, и запрос можно отправить снова. На одно подтверждение очередь отводит до 24 часов.

Маршрут также доступен в [Swagger UI](http://127.0.0.1:8000/docs).

## Запуск через CLI

Команда для прямого импорта из корня `DeepSemantic`.

```sh
docker compose run --build --rm arxiv-grabber --from 2023-06-05 --to 2023-06-05 --query 'cat:cs.AI'
```

Обе даты включены. Отбор идёт по дате первой подачи статьи на arXiv в UTC. При пустом фильтре берутся все категории. Бакет по умолчанию — `arxiv-papers`, его можно изменить переменной `RUSTFS_BUCKET`.

PDF сохраняются под ключом `arxiv/<arxiv_id>.pdf`, где `arxiv_id` включает версию статьи. Уже существующий объект используется повторно. Если отдельный PDF не загрузился, скрипт обрабатывает оставшиеся статьи и возвращает код `1`. Повторный запуск проверяет наличие объектов и догружает пропущенные PDF.

## Каталог в pgAdmin

В [pgAdmin](http://127.0.0.1:5050) откройте **DeepSemantic PostgreSQL → Databases → deepsemantic → Schemas → public → Tables → arxiv_downloads**. В запущенном pgAdmin обновите узел **Tables**, чтобы увидеть новую таблицу.

```sql
SELECT arxiv_id, title, submitted_at, date_from, date_to,
       bucket, object_key, downloaded_at
FROM public.arxiv_downloads
ORDER BY downloaded_at DESC, arxiv_id;
```

Строка появляется после подтверждения, что PDF есть в RustFS. `date_from` и `date_to` — границы запроса, `submitted_at` — дата и время первой подачи, `downloaded_at` — время загрузки объекта в бакет по данным RustFS. Повторный запуск с тем же диапазоном и бакетом использует ту же строку. Для другого диапазона создаётся отдельная запись о статье, а PDF в том же бакете используется повторно.

Запросы к arXiv выполняются последовательно с интервалом не менее трёх секунд. Для массовой загрузки архивов arXiv предоставляет [доступ к PDF через S3](https://info.arxiv.org/help/bulk_data_s3.html). [Руководство по API arXiv](https://info.arxiv.org/help/api/user-manual.html) · [Условия использования API](https://info.arxiv.org/help/api/tou.html).
