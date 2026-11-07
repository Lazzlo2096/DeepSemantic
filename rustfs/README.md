# RustFS

Для отдельного запуска из корня проекта выполните `docker compose up -d rustfs`.

- S3 API доступен на `http://127.0.0.1:9000`. Контейнеры Compose обращаются к `http://rustfs:9000`.
- [Консоль RustFS](http://127.0.0.1:9001) использует ключ доступа `deepsemantic` и секретный ключ `deepsemantic-local` при локальном запуске. Их можно переопределить через `RUSTFS_ACCESS_KEY` и `RUSTFS_SECRET_KEY`.
- Порты хоста можно переопределить через `RUSTFS_API_PORT` и `RUSTFS_CONSOLE_PORT`.
- Бакеты и объекты хранятся в томе Docker `rustfs-data`.

[Документация RustFS для Docker](https://docs.rustfs.com/en/installation/container/docker).
