.PHONY: up down logs images test lint demo

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f api sandbox-runner

# Pre-pull sandbox images so the first submission does not hit its timeout while Docker downloads them.
images:
	docker pull python:3.11-slim
	docker pull node:20-slim
	docker pull eclipse-temurin:21-jdk

lint:
	ruff check .

test:
	python3 -m pytest -q

demo:
	./scripts/demo.sh
