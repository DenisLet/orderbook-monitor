# Базовый образ Python
FROM python:3.11-slim

# Рабочая директория
WORKDIR /app

# Устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем скрипт
COPY monitor.py .

# Опционально: простой healthcheck на порт 8000
RUN mkdir -p /app/health
COPY <<EOF /app/health/health.py
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_server():
    server = HTTPServer(("0.0.0.0", 8000), Handler)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()
EOF

# Команда запуска скрипта (вместо uvicorn)
CMD ["sh", "-c", "python /app/health/health.py && python /app/monitor.py"]
