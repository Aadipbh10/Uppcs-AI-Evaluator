FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Fail the image build if the Mini App is missing.
RUN test -f /app/static/branding/mini_app.html

CMD ["python", "start.py"]
