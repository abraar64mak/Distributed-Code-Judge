FROM python:3.11-slim

WORKDIR /app

COPY services/sandbox-runner/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/sandbox-runner/ ./services/sandbox-runner/

CMD ["python", "services/sandbox-runner/runner.py"]
