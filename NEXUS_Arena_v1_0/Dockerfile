FROM python:3.12-slim
WORKDIR /app
COPY server/requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r /app/server/requirements.txt
COPY server /app/server
COPY web /app/web
ENV NEXUS_DB=/data/arena.sqlite3
RUN mkdir -p /data
EXPOSE 8000
CMD ["sh","-c","exec uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers"]
