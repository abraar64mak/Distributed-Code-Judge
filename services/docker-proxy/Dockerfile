FROM python:3.11-slim

WORKDIR /app
COPY services/docker-proxy/proxy.py .

EXPOSE 2375
# Needs access to the Docker socket (mounted read-only by compose); standard library only.
CMD ["python", "-u", "proxy.py"]
