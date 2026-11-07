# arXiv Grabber

Из корня `DeepSemantic` выполните:

```sh
docker compose run --build --rm arxiv-grabber --from 2023-06-05 --to 2023-06-05 --query 'cat:cs.AI'
```

Обе даты включены. Отбор идёт по дате первой подачи статьи на arXiv в UTC. Без `--query` берутся все категории. Параметр `--bucket` задаёт бакет для конкретного запуска. Бакет по умолчанию — `arxiv-papers`, его можно изменить переменной `RUSTFS_BUCKET`.

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
