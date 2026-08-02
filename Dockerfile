# Dockerfile for Podarcis Lightweight User Container (podarcis-user:latest)
FROM python:3.12-slim

USER root

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv package manager
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Create non-root podarcis user
RUN useradd -m -u 1000 podarcis && \
    mkdir -p /home/podarcis/workspace && \
    chown -R podarcis:podarcis /home/podarcis

USER podarcis
WORKDIR /home/podarcis/workspace

EXPOSE 8000

# Default command starts Python HTTP / Web workspace server inside user container
CMD ["python3", "-m", "http.server", "8000"]
