FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libpango-1.0-0 libpangoft2-1.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY alembic.ini .
COPY alembic alembic
COPY bot.py .
COPY docker-start.sh .
RUN chmod +x docker-start.sh
EXPOSE 9005
CMD ["./docker-start.sh"]
