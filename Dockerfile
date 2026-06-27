# Proof Matrix UI + API — case_ui/app.py (pure-stdlib HTTP server).
# Mirrors render.yaml: pip install -r requirements.txt, then python case_ui/app.py.
FROM python:3.12.7-slim

# Don't write .pyc files; flush stdout/stderr so container logs stream live.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8001

WORKDIR /app

# Install deps first so this layer caches unless requirements.txt changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code + the committed data snapshot it falls back to on deploy hosts.
COPY case_ui/ ./case_ui/
COPY caselib.py ./

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8001

# app.py reads $PORT and binds 0.0.0.0.
CMD ["python", "case_ui/app.py"]
