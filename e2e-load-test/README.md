# Нагрузочные тесты REST API

Перед прогоном только основной ветки задайте глобальный режим:

```http
PUT /pipeline/settings
Content-Type: application/json

{"summary_mode":"deferred"}
```

Создавайте импорты через `POST /arxiv/imports`. Завершение импорта означает
завершение скачивания, а не индексации. Для каждой ожидаемой статьи опрашивайте
`GET /papers/{arxiv_id}/processing` до `article_index.status == "completed"`.
`summary_index.status` для отсутствующих саммари будет `deferred`.

После прогона включите обработку накопившихся статей:

```http
PUT /pipeline/settings
Content-Type: application/json

{"summary_mode":"enabled"}
```

Для полной готовности ожидайте `summary_index.status == "completed"` у всех
ожидаемых статей. Прогресс фоновой сверки доступен через
`GET /pipeline/summary-reconciliation`; настройки — `GET /pipeline/settings`.
Через frontend добавьте префикс `/api` ко всем путям.

Настройка действует на всю систему и сохраняется после перезапуска.
Уже выполняющийся вызов LLM или батч может завершиться после переключения.
Для измерения изолированной основной ветки переключайте режим до отправки
импортов. Не запускайте одновременно тесты, требующие разных глобальных режимов.

Проверка переключения и восстановления на настоящих Temporal/PostgreSQL/
RustFS/Qdrant с подменой внешних LLM и эмбеддингов: `just temporal-test`.
