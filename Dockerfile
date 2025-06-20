FROM python:3.12-slim

WORKDIR /app
RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY services.py .
COPY forms.py .
COPY models.py .
COPY config.py .
COPY templates/ templates/
COPY static/ static/

RUN mkdir -p /tmp /app/instance /app/static/uploads /app/test_data /app/logs && chmod 777 /tmp /app/instance /app/static/uploads /app/test_data /app/logs

EXPOSE 5000
CMD ["/app/venv/bin/gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "app:app"]
