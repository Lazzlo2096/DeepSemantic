FROM python:3.11-slim

WORKDIR /migrations
RUN python -m pip install --no-cache-dir 'alembic>=1.16,<2' 'psycopg[binary]>=3.2,<4'

COPY alembic.ini env.py script.py.mako ./
COPY versions/ ./versions/

ENTRYPOINT ["alembic"]
CMD ["upgrade", "head"]
