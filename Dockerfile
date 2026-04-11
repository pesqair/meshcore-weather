FROM python:3.12-slim

WORKDIR /app

# pyIEM pulls in a scientific-Python stack (numpy, pandas, shapely, pyproj,
# matplotlib, metpy, etc.). Most have wheels on PyPI for x86_64/arm64, but
# pygrib needs libeccodes at build time if no wheel is available on the host
# architecture. Install the system libs to be safe.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libeccodes0 \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

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
