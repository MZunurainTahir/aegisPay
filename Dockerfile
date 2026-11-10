FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY gateway/ ./gateway/
COPY agent/ ./agent/

# Pre-trained model is committed; no training step needed
# Expose port
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn gateway.main:app --host 0.0.0.0 --port $PORT"]

