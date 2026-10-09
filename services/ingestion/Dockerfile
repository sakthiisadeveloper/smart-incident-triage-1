FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY libs/common /opt/libs/common
COPY services/ingestion/requirements.txt .
RUN pip install /opt/libs/common -r requirements.txt

COPY services/ingestion/ingestion_service ./ingestion_service

RUN useradd --create-home --uid 10001 app
USER app
EXPOSE 8000
CMD ["uvicorn", "ingestion_service.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
