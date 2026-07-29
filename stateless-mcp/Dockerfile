FROM node:24-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY requirements.txt requirements-dev.txt ./
RUN python3 -m pip install --break-system-packages --no-cache-dir -r requirements.txt -r requirements-dev.txt

COPY stateless-mcp/package.json stateless-mcp/package-lock.json ./stateless-mcp/
RUN npm ci --prefix stateless-mcp

COPY . .
RUN npm run build --prefix stateless-mcp

ENV MCP_PORT=8010 \
    MCP_WORKSPACE_ROOT=/workspace \
    PYTHON_EXECUTABLE=python3

EXPOSE 8010

CMD ["node", "stateless-mcp/dist/src/server.js"]
