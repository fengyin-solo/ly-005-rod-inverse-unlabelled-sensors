FROM python:3.13.15-slim-bookworm@sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY app/ /app/

RUN mkdir -p /state /logs/verifier \
    && chown -R nobody:nogroup /app /state /logs/verifier \
    && chmod -R u+rwX,go+rX /app /state

WORKDIR /app

CMD ["sleep", "infinity"]
