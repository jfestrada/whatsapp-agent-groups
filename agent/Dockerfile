FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml agent.py ./
RUN pip install --no-cache-dir .

ENV MCP_URL=http://whatsapp-mcp:8000/mcp \
    DEEPSEEK_BASE_URL=https://api.deepseek.com \
    DEEPSEEK_MODEL=deepseek-chat \
    DIGEST_TIMEZONE=Europe/Madrid \
    AGENT_HOST=0.0.0.0 \
    AGENT_PORT=8081

EXPOSE 8081

CMD ["python", "-m", "agent"]
