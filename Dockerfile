FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn[standard] asyncpg
COPY weather_service.py /app/weather_service.py
EXPOSE 8086
CMD ["uvicorn", "weather_service:app", "--host", "0.0.0.0", "--port", "8086"]
