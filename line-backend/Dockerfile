FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

EXPOSE 8000

CMD ["sh", "-c", "python migrate_add_schedule_reminder_sent_at_column.py && uvicorn app.main:app --host 0.0.0.0 --port 8000"]