FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install deps
COPY app/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/app ./app
COPY app/ops ./ops

EXPOSE 8070

CMD ["bash","-lc","python -m app.migrate && uvicorn app.main:app --host 0.0.0.0 --port 8070"]

