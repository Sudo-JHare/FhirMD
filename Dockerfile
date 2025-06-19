FROM tomcat:10.1-jdk17

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv curl coreutils \
    && curl -fsSL https://deb.nodesource.com/setup_18.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

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

RUN mkdir -p /tmp /app/h2-data /app/static/uploads /app/test_data /app/logs && chmod 777 /tmp /app/h2-data /app/static/uploads /app/test_data /app/logs

RUN pip install supervisor
COPY supervisord.conf /etc/supervisord.conf

EXPOSE 5000
CMD ["supervisord", "-c", "/etc/supervisord.conf"]