# Логи DeepSemantic

`just up` запускает Grafana Alloy, Loki и Grafana. Для уже запущенного стека можно отдельно выполнить `docker compose up -d loki alloy grafana`.

Alloy читает логи `arxiv-grabber`, `paper-indexer`, `api` и `embeding-service` из текущего проекта Docker Compose. Loki хранит их семь дней в томе `loki-data`. Alloy сохраняет позиции чтения в томе `alloy-data`, а настройки Grafana хранятся в томе `grafana-data`.

Открой Grafana, войди с логином `admin` и паролем `deepsemantic`, затем выбери **Explore → Loki**. Порт и начальные учётные данные можно задать через `GRAFANA_PORT`, `GRAFANA_ADMIN_USER` и `GRAFANA_ADMIN_PASSWORD`. [Grafana Explore](http://127.0.0.1:3000/explore)

Запрос LogQL для общего потока:

```logql
{service_name=~"arxiv-grabber|paper-indexer|api|embeding-service"}
```

Переходы статуса статьи можно найти по полям JSON:

```logql
{service_name=~"arxiv-grabber|paper-indexer|api"}
| json
| event=~"arxiv_papers[.](insert|update)"
| table="arxiv_papers"
| arxiv_id="2611.07970v1"
```

При первой записи статьи воркер пишет `arxiv_papers.insert` с `operation="insert"` и `null` в `changes.status.old`. При смене статуса пишется `arxiv_papers.update` с `operation="update"` и значениями `old` и `new` в `changes.status`. Оба события записываются после коммита в PostgreSQL. Добавь `| changes_status_new="embeddinged"`, чтобы увидеть завершение индексации.

Чтобы увидеть вставки и обновления `arxiv_imports`, в **Explore → Loki** переключи редактор на **Code**, выбери диапазон времени и нажми **Run query** после ввода запроса.

```logql
{service_name=~"arxiv-grabber|api"}
|= `"arxiv_imports"`
| json
| table="arxiv_imports"
```

Событие `arxiv_import.insert` содержит `operation="insert"` и сохранённую строку в `row`. Событие `arxiv_import.update` содержит `operation="update"` и изменившиеся колонки с полями `old` и `new` в `changes`. API и воркер пишут эти события после коммита. Начало попытки видно по изменению `attempts`, а её завершение по переходу статуса из `running`. Добавь `| event="arxiv_import.update" | changes_status_new="failed"`, чтобы увидеть переходы в `failed`.

Для задания импорта используй его ID из ответа `POST /arxiv/imports`. Чтобы отфильтровать запрос выше, добавь `| import_id="…"`. Чтобы увидеть и другие события задания, выполни такой запрос:

```logql
{service_name=~"arxiv-grabber|api"}
| json
| import_id="4411913797a7461a864a0581086d4765"
```

По этому фильтру видны постановка в очередь, начало попытки (`arxiv_import.started`), итог попытки со счётчиками и ошибкой (`arxiv_import.attempt_finished`), а также ошибки обработки отдельных статей и их переходы статуса. `paper-indexer` индексирует статьи отдельно и связывает свои события со статьёй через `arxiv_id`.
