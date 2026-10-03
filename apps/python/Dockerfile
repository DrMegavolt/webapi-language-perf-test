FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chmod=0755 entrypoint.sh /app/entrypoint.sh
COPY app/ /app/app/

RUN useradd --system --create-home appuser
USER appuser

EXPOSE 8080

CMD ["/app/entrypoint.sh"]
