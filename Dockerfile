# Use Python 3.11 (Stable and compatible with Playwright)
FROM python:3.11-slim

# 1. Install system dependencies required for Playwright and Chrome
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory inside the container
WORKDIR /app

# 2. Copy requirements and install Python libraries
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Install Playwright browsers (Chromium is enough for this project)
RUN playwright install chromium
RUN playwright install-deps

# Copy the rest of your application code
COPY . .

# 4. Start the server
# usage: uvicorn main:app --host 0.0.0.0 --port 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
