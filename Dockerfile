# Use the latest official Python image
FROM python:latest

# Environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1 \
  PORT=3081

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
  curl \
  build-essential \
  && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

ARG UID=10001
RUN adduser --disabled-password --gecos "" --home "/home/appuser" --shell "/sbin/nologin" --uid "${UID}" appuser

# Copy app code
COPY . .


USER appuser

CMD ["python3", "main.py"]
