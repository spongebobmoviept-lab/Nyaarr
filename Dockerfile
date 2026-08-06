FROM python:3.12-slim

RUN useradd --create-home --uid 1000 nyaarr
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

RUN mkdir -p /data && chown -R nyaarr:nyaarr /app /data
USER nyaarr

ENV DATA_DIR=/data
EXPOSE 8686

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8686"]
