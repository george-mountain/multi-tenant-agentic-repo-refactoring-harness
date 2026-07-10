FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pytest ruff mypy \
    && npm install -g @ast-grep/cli

RUN useradd -m -u 1000 runner \
    && mkdir -p /workspace \
    && chown runner:runner /workspace

WORKDIR /workspace
USER runner

RUN git config --global user.email "agent@harness.local" \
    && git config --global user.name "Refactor Harness Agent" \
    && git config --global init.defaultBranch main

CMD ["sleep", "infinity"]
