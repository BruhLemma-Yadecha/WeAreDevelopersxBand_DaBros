FROM python:3.14-slim

WORKDIR /app
COPY app.py ./app.py
EXPOSE 8080
VOLUME ["/data"]
CMD ["python3", "app.py", "--data", "/data/pocketful.json"]
