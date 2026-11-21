import sys

from alembic.config import main


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if arguments == ["upgrade", "head"]:
        # The application's RAG ownership migration references auth.users.
        main(argv=["--name", "auth", "upgrade", "head"])
    main(argv=arguments)
