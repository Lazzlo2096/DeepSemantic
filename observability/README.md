# Логи DeepSemantic

`just up` запускает Grafana Alloy, Loki и Grafana. Для уже запущенного стека можно отдельно выполнить `docker compose up -d loki alloy grafana`.

Alloy читает логи `arxiv-grabber`, `paper-indexer`, `api` и `embeding-service` из текущего проекта Docker Compose. Loki хранит их семь дней в томе `loki-data`. Alloy сохраняет позиции чтения в томе `alloy-data`, а настройки Grafana хранятся в томе `grafana-data`.

Открой Grafana, войди с логином `admin` и паролем `deepsemantic`, затем выбери **Explore → Loki**. Порт и начальные учётные данные можно задать через `GRAFANA_PORT`, `GRAFANA_ADMIN_USER` и `GRAFANA_ADMIN_PASSWORD`. [Grafana Explore](http://127.0.0.1:3000/explore)

Запрос LogQL для общего потока:

```logql
{service_name=~"arxiv-grabber|paper-indexer|api|embeding-service"}
```

К запросу можно добавить `|= "2611.07970v1"`, чтобы найти строки, содержащие ID статьи.
