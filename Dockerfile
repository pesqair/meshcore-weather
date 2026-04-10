FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY meshcore_weather/ meshcore_weather/

RUN pip install --no-cache-dir ".[radar,portal]"

EXPOSE 8080

# Non-root user with dialout group for serial access
RUN useradd -m -s /bin/bash mcw && \
    usermod -aG dialout mcw && \
    mkdir -p /app/data/emwin_cache && \
    chown -R mcw:mcw /app/data

USER mcw

VOLUME ["/app/data"]

ENTRYPOINT ["meshcore-weather"]
