FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /service
COPY pyproject.toml requirements.lock ./
COPY app ./app
RUN pip install -r requirements.lock && pip install --no-deps . && useradd --create-home appuser
COPY alembic.ini ./
COPY alembic ./alembic
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS test
USER root
COPY requirements-dev.lock ./
RUN pip install -r requirements-dev.lock
COPY tests ./tests
USER appuser
CMD ["pytest", "-q", "-p", "no:cacheprovider"]
