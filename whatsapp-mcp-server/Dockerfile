FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml main.py whatsapp.py audio.py whisper_client.py ./
RUN pip install --no-cache-dir .

ENV MESSAGES_DB_PATH=/data/messages.db \
    WHATSAPP_API_BASE_URL=http://whatsapp-bridge:8080/api \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

EXPOSE 8000

CMD ["python", "-m", "main"]
