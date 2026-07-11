FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY atlas ./atlas
COPY migrations ./migrations
COPY alembic.ini seeds ./
RUN pip install --no-cache-dir -e .
EXPOSE 8000
CMD ["uvicorn", "atlas.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
