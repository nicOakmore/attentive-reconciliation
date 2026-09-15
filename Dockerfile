FROM python:3.12-slim
# RapidOCR reads these scanned statements accurately; tesseract stays as a fallback. libgomp is needed by
# onnxruntime and libglib by the opencv build it loads.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr libtesseract-dev libgomp1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=10000 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    PDF_WORKERS=2
# One worker on purpose: a run lives in process memory, and a second worker would answer the status poll and the
# docx download from a process that never saw the job.
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 16 --timeout 3600
