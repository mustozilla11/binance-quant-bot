# Python 3.12 slim tabanlı hafif imaj
FROM python:3.12-slim

# Çalışma dizini
WORKDIR /app

# Sistem paketlerini güncelle ve build araçlarını kur
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    git \
    && rm -rf /var/lib/apt/lists/*

# Bağımlılıkları yükle
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Uygulama dosyalarını kopyala
COPY . .

# Botu başlat
CMD ["python", "bot.py"]
