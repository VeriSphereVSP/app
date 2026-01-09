FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY ops ./ops
EXPOSE 8070
CMD ["bash","-lc","python -m app.migrate && uvicorn app.main:app --host 0.0.0.0 --port 8070"]
