FROM nvidia/cuda:12.6.0-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive TZ=America/Santiago

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.12 \
    python3.12-dev \
    python3.12-venv \
    python3-pip \
    gcc \
    libpq-dev \
    ffmpeg \
    curl \
    libgl1 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.12 1 \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

RUN rm -rf /usr/local/cuda-12/compat \
    && ln -sf /usr/lib/x86_64-linux-gnu/libcudnn.so.9 /usr/local/cuda/lib64/libcudnn.so \
    && ln -sf /usr/lib/x86_64-linux-gnu/libcudnn.so.9 /usr/local/cuda/lib64/libcudnn.so.9 \
    && ln -sf /usr/local/lib/python3.12/site-packages/wsdl /usr/local/lib/python3.12/dist-packages/wsdl \
    && ldconfig

COPY . .

RUN mkdir -p /app/static/vendor/tabler /app/static/vendor/tabler-icons/fonts /app/static/vendor/p5 \
 && curl -sfL -o /app/static/vendor/tabler/tabler.min.css \
       "https://cdn.jsdelivr.net/npm/@tabler/core@1.4.0/dist/css/tabler.min.css" \
 && curl -sfL -o /app/static/vendor/tabler/tabler.min.js \
       "https://cdn.jsdelivr.net/npm/@tabler/core@1.4.0/dist/js/tabler.min.js" \
 && curl -sfL -o /app/static/vendor/tabler-icons/tabler-icons.min.css \
       "https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.43.0/dist/tabler-icons.min.css" \
 && curl -sfL -o /app/static/vendor/tabler-icons/fonts/tabler-icons.woff2 \
       "https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.43.0/dist/fonts/tabler-icons.woff2" \
 && curl -sfL -o /app/static/vendor/tabler-icons/fonts/tabler-icons.woff \
       "https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.43.0/dist/fonts/tabler-icons.woff" \
 && curl -sfL -o /app/static/vendor/p5/p5.min.js \
       "https://cdn.jsdelivr.net/npm/p5@1.11.3/lib/p5.min.js"

RUN python manage.py collectstatic --noinput 2>&1 | grep -v "^$" || true

EXPOSE 8000 8001

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "4"]
