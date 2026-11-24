# Everything the test suite needs, including a real browser (Chromium) for
# the phone pages. Build + run via scripts/test.sh.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
    numpy pytest playwright==1.49.1 esprima
WORKDIR /repo
ENV PYTHONDONTWRITEBYTECODE=1
