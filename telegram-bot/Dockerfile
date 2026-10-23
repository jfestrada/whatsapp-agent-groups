FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml bot.py schedule.py ./
RUN pip install --no-cache-dir .

ENV AGENT_URL=http://agent:8081

CMD ["python", "-m", "bot"]
