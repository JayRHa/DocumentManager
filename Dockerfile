# Document Manager - Optimized Production Image
# Multi-stage build for minimal final image size

# Stage 1: Build dependencies
FROM python:3.12-slim AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    gcc \
    g++ \
    libmagic-dev \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy and install Python dependencies
WORKDIR /build
COPY requirements.txt .

# Upgrade pip and install dependencies
RUN pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# Stage 2: Runtime image
FROM python:3.12-slim AS runtime

# Install runtime dependencies only
RUN apt-get update && apt-get install -y \
    # Tesseract OCR and languages
    tesseract-ocr \
    tesseract-ocr-deu \
    tesseract-ocr-eng \
    # PDF utilities
    poppler-utils \
    # Image processing libraries
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    # File type detection
    libmagic1 \
    # Health check
    curl \
    # Process management for all-in-one mode
    supervisor \
    # Clean up
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Copy Python environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Create non-root user with specific UID for consistency
RUN groupadd -r -g 1000 appuser && \
    useradd -r -u 1000 -g appuser -m -d /home/appuser -s /bin/bash appuser

# Set working directory
WORKDIR /app

# Copy application code
COPY --chown=appuser:appuser app/ ./app/
COPY --chown=appuser:appuser frontend/ ./frontend/
COPY --chown=appuser:appuser docker-entrypoint.sh ./
COPY --chown=appuser:appuser docker-entrypoint-aio.sh ./
COPY --chown=appuser:appuser supervisord.conf /etc/supervisor/conf.d/

# Create necessary directories with correct permissions
RUN mkdir -p data data/logs data/staging data/storage data/uploads backups chroma && \
    chmod +x docker-entrypoint.sh docker-entrypoint-aio.sh && \
    chown -R appuser:appuser /app && \
    mkdir -p /var/log/supervisor && \
    chown -R appuser:appuser /var/log/supervisor

# Switch to non-root user
USER appuser

# Runtime environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    # Tesseract and Poppler paths
    TESSERACT_PATH=/usr/bin/tesseract \
    POPPLER_PATH=/usr/bin \
    # Application settings (should be overridden in production)
    DATABASE_URL=sqlite:///./data/documents.db \
    SECRET_KEY=MUST-BE-SET-IN-PRODUCTION \
    AI_PROVIDER=openai \
    TRUSTED_PROXY_IPS=127.0.0.1

# Add metadata labels
LABEL maintainer="Document Manager Team" \
      version="1.0.0" \
      description="AI-powered document management system" \
      org.opencontainers.image.source="https://github.com/yourusername/documentmanager"

# Expose application port
EXPOSE 8000

# Health check with proper timing
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

# Use entrypoint script for initialization
ENTRYPOINT ["/app/docker-entrypoint.sh"]
