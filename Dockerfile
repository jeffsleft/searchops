FROM python:3.12-slim
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SQLite lives on a mounted volume so it survives container restarts.
# Override DATABASE_PATH if you prefer a different location.
ENV DATABASE_PATH=/srv/data/recruiting.db

EXPOSE 8000

# Single worker required for SQLite WAL consistency.
# Move to Postgres and raise --workers if you need horizontal scale.
CMD ["gunicorn", "app.asgi:app", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "-b", "0.0.0.0:8000", \
     "--workers", "1", \
     "--timeout", "120"]
