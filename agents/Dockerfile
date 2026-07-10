FROM python:3.12-slim

WORKDIR /app
COPY agents/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/harness_shared /app/harness_shared
COPY agents/worker /app/worker

ENV PYTHONPATH=/app
EXPOSE 9100
CMD ["python", "-m", "worker.main"]
