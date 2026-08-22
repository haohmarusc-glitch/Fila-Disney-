FROM python:3.12-slim

ARG APP_GIT_SHA=unknown
ENV APP_GIT_SHA=${APP_GIT_SHA}

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py notifier.py localizacao.py analyze.py healthcheck.py coords.py watchlist.json ./
# coords.json é opcional e gerado depois; o COPY não pode falhar sem ele
COPY coords.jso[n] ./

# Usuário não-root. O data/ é volume e precisa pertencer a ele.
RUN useradd --create-home --uid 10001 fila \
    && mkdir -p /app/data \
    && chown -R fila:fila /app
USER fila

# Container "up" com o loop travado não grava nada: o healthcheck olha o banco.
HEALTHCHECK --interval=5m --timeout=10s --start-period=2m --retries=3 \
    CMD ["python", "healthcheck.py"]

CMD ["python", "-u", "monitor.py"]
