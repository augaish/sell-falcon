FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    SELLFALCON_DATA=/data SELLFALCON_SECURE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py discovery.py hosted.py provider.py spend.py synthetic_batch.py server.py manage.py ./
COPY static/ static/

# القرص الدائم يُركَّب على /data. بدونه تضيع البيانات عند كل إعادة نشر.
RUN useradd --create-home --uid 10001 sellfalcon && mkdir -p /data && chown sellfalcon:sellfalcon /data
USER sellfalcon
VOLUME /data

# عملية واحدة عمدًا (انظر server.py). PORT تضبطه الاستضافة.
CMD ["sh", "-c", "exec uvicorn server:application --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
