# DeepSemantic Auth

`auth-service` хранит аккаунты и серверные сессии в схеме `auth` основной базы PostgreSQL. Caddy вызывает `/verify` перед защищёнными API-маршрутами и передаёт подтверждённые поля пользователя в основной FastAPI.

После применения миграций создайте первого администратора интерактивной командой:

```sh
docker compose run --rm auth-service python -m auth_service.cli create-admin admin@example.com
```

Для серверного Compose добавьте второй файл:

```sh
docker compose -f compose.yaml -f compose.server.yaml run --rm auth-service \
  python -m auth_service.cli create-admin admin@example.com
```

Пароль вводится без отображения и сохраняется как Argon2id-хеш. Последующих пользователей администратор приглашает со страницы `/users.html`. Ссылка приглашения показывается один раз, а PostgreSQL хранит только её SHA-256-хеш.

Браузерная сессия состоит из `HttpOnly` cookie с секретом сессии и отдельного CSRF-токена. В PostgreSQL сохраняются только SHA-256-хеши обоих значений. При отключении аккаунта все его активные сессии отзываются.

На сервере обязательно задайте случайный `AUTH_PROXY_SECRET`. Compose использует его для доверенного канала Caddy → FastAPI. Локальный запуск использует значение для разработки.
