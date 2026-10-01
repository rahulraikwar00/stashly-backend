FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# PORT is supplied by the platform. Render injects 10000; do not hardcode it
# here, or the container listens on a port the platform is not proxying to.
ENV PORT=10000

EXPOSE 10000

# No IG_* or DATABASE_URL baked in: those are platform secrets, never image
# layers. Supply them as environment variables at runtime.
CMD ["python", "-m", "app"]