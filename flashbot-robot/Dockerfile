FROM python:3.10-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
EXPOSE 5000
CMD ["python", "-u", "run.py"]