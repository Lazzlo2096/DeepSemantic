import argparse
import getpass
import os

import psycopg

from .security import hash_password, normalize_email


def connection() -> psycopg.Connection:
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "postgres"),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )


def create_admin(email: str) -> None:
    try:
        normalized = normalize_email(email)
    except ValueError as error:
        raise SystemExit("Укажите корректный адрес электронной почты.") from error
    password = getpass.getpass("Новый пароль администратора: ")
    confirmation = getpass.getpass("Повторите пароль: ")
    if password != confirmation:
        raise SystemExit("Пароли не совпадают.")
    if len(password) < 12:
        raise SystemExit("Пароль должен содержать не менее 12 символов.")
    with connection() as database:
        database.execute("""
            INSERT INTO auth.users (email, password_hash, role)
            VALUES (%s, %s, 'admin')
            ON CONFLICT (lower(email)) DO UPDATE
            SET password_hash = EXCLUDED.password_hash, role = 'admin', disabled_at = NULL
        """, (normalized, hash_password(password)))
        database.execute("""
            UPDATE auth.sessions SET revoked_at = now()
            WHERE user_id = (SELECT id FROM auth.users WHERE lower(email) = %s)
              AND revoked_at IS NULL
        """, (normalized,))
    print(f"Администратор {normalized} создан или обновлён.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Управление аккаунтами DeepSemantic")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-admin", help="создать или восстановить администратора")
    create.add_argument("email")
    arguments = parser.parse_args()
    if arguments.command == "create-admin":
        create_admin(arguments.email)


if __name__ == "__main__":
    main()
