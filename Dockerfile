FROM python:3.15-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN useradd -r -u 1001 appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:build", "--factory", "--host", "0.0.0.0", "--port", "8000"]
