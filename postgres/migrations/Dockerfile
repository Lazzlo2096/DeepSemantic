FROM python:3.11-slim

WORKDIR /migrations
RUN python -m pip install --no-cache-dir 'alembic>=1.16,<2' 'psycopg[binary]>=3.2,<4'

COPY alembic.ini env.py entrypoint.py script.py.mako ./
COPY versions/ ./versions/
COPY auth_versions/ ./auth_versions/

ENTRYPOINT ["python", "entrypoint.py"]
CMD ["upgrade", "head"]
