FROM python:3.14-slim

WORKDIR /app

COPY . .

ENV PYTHONUNBUFFERED=1

EXPOSE 8787

CMD ["python", "-u", "railway_start.py"]
