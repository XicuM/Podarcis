# Dockerfile for Podarcis Code-Server Web Workspace
FROM codercom/code-server:latest

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install uv Python environment manager
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install OpenCode CLI for AI assistant integration
RUN HOME=/home/coder curl -fsSL https://opencode.ai/install | bash && \
    ln -sf /home/coder/.opencode/bin/opencode /usr/local/bin/opencode || true && \
    chown -R coder:coder /home/coder/.opencode || true


# Switch back to coder user
USER coder
WORKDIR /home/coder/workspace
