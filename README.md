# WebScreenCAP

A microservice that captures screenshots and HTML content from URLs asynchronously using **FastAPI** and **Celery**, leveraging **Node.js** with **Playwright** (and stealth/extra plugins) behind the scenes. Captured artifacts (screenshots, HTML) are uploaded to an S3-compatible storage (e.g., Tigris, MinIO, AWS S3).

---

## Table of Contents

- [Features](#features)
- [Architecture Overview](#architecture-overview)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [API Reference](#api-reference)
  - [1. Initiate Screenshot Capture](#1-initiate-screenshot-capture)
  - [2. Get Task Details](#2-get-task-details)
- [Running the Celery Worker](#running-the-celery-worker)
- [Local Development](#local-development)
  - [Installing Dependencies with `dependencies.sh`](#installing-dependencies-with-dependenciessh)
  - [Python Dependencies](#python-dependencies)
- [Dockerization](#dockerization)
  - [Dockerfile for the Celery Worker](#dockerfile-for-the-celery-worker)
  - [Dockerfile for the Main API](#dockerfile-for-the-main-api)
  - [Deploying with Fly.io using Dockerfiles](#deploying-with-flyio-using-dockerfiles)
    - [Setting Environment Variables as Fly Secrets](#setting-environment-variables-as-fly-secrets)
- [Contributing](#contributing)
- [License](#license)

---

## Features

1. **Async Screenshot & HTML Capture**  
   Offload screenshot tasks to Celery for asynchronous processing.

2. **Playwright/Stealth Integration**  
   Uses [playwright-extra](https://www.npmjs.com/package/playwright-extra) plus stealth plugins to reduce detection.

3. **Ad-blocking**  
   Integrates [@ghostery/adblocker-playwright](https://www.npmjs.com/package/@ghostery/adblocker-playwright) for blocking ads and trackers while capturing pages.

4. **S3-Compatible Upload**  
   Captured artifacts are uploaded to Tigris (or any S3-compatible storage configured via environment variables).

5. **API Key Validation**  
   Endpoints require a valid `API_KEY` for added security.

6. **REST API**  
   Built with **FastAPI**, offering straightforward endpoints to initiate and poll screenshot tasks.

---

## Architecture Overview

```
┌────────────────────┐
│   FastAPI App      │
│ (uvicorn/gunicorn) │
│   endpoints        │
└───────┬────────────┘
        │ (HTTP request)
┌───────┴──────────────────────────────────────────────┐
│  Celery Worker                                       │
│ capture_screenshot_and_html → Invokes Node.js script │
└───────┬──────────────────────────────────────────────┘
        │
┌───────┴──────────┐
│   Node.js        │
│   (scraper.js)   │
│  Playwright etc. │
└───────┬──────────┘
        │
        ▼
     S3-Compatible 
      Object Storage
```

1. A user sends an HTTP request to the FastAPI endpoint, including the `url` and `api_key`.
2. FastAPI validates the `api_key`, then dispatches a Celery task.
3. The Celery worker runs the Node.js script (`scraper.js`) to:
    - Launch a headless Chromium with stealth & ad-block.
    - Capture a full-page screenshot & HTML.
    - Upload both artifacts to S3-compatible storage.
4. Celery returns a result object containing the S3 URLs.
5. The user can retrieve the task status (and result) via another endpoint.

---

## Requirements

- **Python** ≥ 3.8
- **Node.js** ≥ 16
- **npm** / **pnpm** / **yarn** for installing Node dependencies
- **Redis** (or another Celery-compatible broker) for message brokering
- **S3-Compatible Object Storage** (e.g., Tigris, MinIO, AWS S3) credentials
- Additional system dependencies needed for Playwright (detailed below)

---

## Installation

1. **Clone the repository**:

   ```bash
   git clone https://github.com/example/webscreencap.git
   cd webscreencap
   ```

2. **Install Python dependencies**:

   ```bash
   pip install -r requirements.txt
   ```

   > The `requirements.txt` file includes:
   > ```text
   > fastapi
   > fastapi_cli
   > uvicorn
   > celery
   > redis
   > flower
   > json5
   > ```
   > These cover our FastAPI framework, Celery, Redis, and other libraries.

3. **Install Node.js dependencies**:
   - If you’re on Arch Linux (or derivative), you can leverage the `dependencies.sh` script, which installs system dependencies plus Node modules via **pnpm**.
   - Otherwise, ensure you have Node.js installed, then run:
     ```bash
     cd <folder-containing-scraper.js>
     npm install
     # or pnpm install
     # or yarn install
     ```
   - Install the same libraries listed in `dependencies.sh` (Playwright, etc.) if not already covered.

4. **Set up or ensure you have a running Celery broker** (e.g., Redis, RabbitMQ).

---

## Configuration

Configure the following environment variables **before** running the application. You can place these in a `.env` file or directly export them in your environment.

| Variable                | Description                                                    | Example                                             |
| ----------------------- | -------------------------------------------------------------- | --------------------------------------------------- |
| `API_KEY`              | Required API Key for FastAPI endpoints                        | `mysecretkey`                                       |
| `AWS_REGION`           | S3 region                                                      | `us-east-1`                                         |
| `AWS_ENDPOINT_URL_S3`  | S3 endpoint URL                                               | `https://tigris.example.com` (or `https://s3.amazonaws.com`) |
| `AWS_ACCESS_KEY_ID`    | S3 access key                                                 | `yourAccessKeyId`                                   |
| `AWS_SECRET_ACCESS_KEY`| S3 secret access key                                          | `yourSecretAccessKey`                               |
| `BUCKET_NAME`          | S3 bucket name                                                | `screenshots-bucket`                               |
| `CELERY_BROKER_URL`    | Celery broker URL (matching `CONFIG["celery"]["broker"]`)     | `redis://localhost:6379/0`                          |
| `CELERY_RESULT_BACKEND`| Celery result backend (matching `CONFIG["celery"]["backend"]`) | `redis://localhost:6379/0`                          |

> **Note**: In the provided code, the Celery configuration is read from `shared.py` → `CONFIG["celery"]`. Make sure to update that file to match these environment variable values or set them in a way that the Python code reads correctly.

---

## Usage

### 1. Start the FastAPI service

With uvicorn:

```bash
uvicorn main_api:app --host 0.0.0.0 --port 31137
```

- `main_api:app` references your FastAPI application file/module and the `app` object within it.
- The example config in the code uses `if __name__ == "__main__": uvicorn.run(app, ... )` to start the server.  
- Adjust host/port as necessary.

### 2. Start the Celery worker

In another terminal:

```bash
celery -A celery_task.celery_app worker --loglevel=info
```

Where:
- `-A celery_task.celery_app` matches the Celery app instance in your `celery_task.py`.

Now you have the API and the Celery worker up and running.

---

## API Reference

Below are the primary endpoints. All endpoints require the `api_key` query parameter to match your environment’s `API_KEY`.

### 1. Initiate Screenshot Capture

**Endpoint**:  
```
GET /api/task/screenshot
```

**Query Parameters**:
- `url`: The target URL to capture.
- `api_key`: Your configured API key.

**Example**:
```bash
curl "http://127.0.0.1:31137/webscreencap/api/task/screenshot?url=https://example.com&api_key=mysecretkey"
```

**Response** (JSON):
```json
{
  "id": "<celery_task_id>",
  "message": "Task started successfully"
}
```
- `id` is the unique Celery task identifier.

### 2. Get Task Details

**Endpoint**:  
```
GET /api/task_details/{task_id}
```

**Path Parameters**:
- `task_id`: The task ID from the previous step.

**Query Parameters**:
- `api_key`: Your configured API key.

**Example**:
```bash
curl "http://127.0.0.1:31137/webscreencap/api/task_details/abc123?api_key=mysecretkey"
```

**Response** (JSON):
```json
{
  "task_id": "abc123",
  "status": "SUCCESS",
  "result": {
    "status": "success",
    "message": "Screenshot and HTML processed successfully",
    "screenshot_url": "https://<S3-endpoint>/screenshots-bucket/screenshot-<timestamp>-<hash>.png",
    "html_url": "https://<S3-endpoint>/screenshots-bucket/page-<timestamp>-<hash>.html"
  }
}
```

---

## Running the Celery Worker

The Celery worker is defined in **`celery_task.py`**:

```bash
celery -A celery_task.celery_app worker --loglevel=info
```

- **`celery_task.celery_app`** references the Celery app object.
- Make sure your environment variables are set so the Node.js script can access them.

---

## Local Development

For local development:

1. **Clone** this repository.
2. **Install** dependencies as described or use the included scripts.
3. Configure environment variables or set up a `.env` file.  
4. **Run** the FastAPI server and Celery worker.
5. **Test** with cURL or a REST client (Postman, Insomnia, etc.).

### Installing Dependencies with `dependencies.sh`

If you are on an **Arch Linux** (or derivative) system, we include a `dependencies.sh` script that installs necessary system packages for Node.js, Playwright, and Puppeteer:

```bash
sudo pacman -S nodejs npm pnpm --noconfirm --needed
# Required by Puppeteer/Playwright.
sudo pacman -S atk at-spi2-atk libcups libxkbcommon libxcomposite alsa-lib --noconfirm --needed

# Now run pnpm-based installations
pnpm install playwright
pnpm install express
pnpm install playwright-extra
pnpm install puppeteer-extra-plugin-stealth
pnpm install cross-fetch
pnpm install @ghostery/adblocker-playwright
pnpm install valid-url
pnpm install @aws-sdk/client-s3
pnpm install random-useragent
pnpm playwright install chromium
```

You can adapt this script for other package managers / distributions as needed.

### Python Dependencies

Our `requirements.txt` includes the following:

```
fastapi
fastapi_cli
uvicorn
celery
redis
flower
json5
```

Install them via:

```bash
pip install -r requirements.txt
```

---

## Dockerization

We provide two separate Dockerfiles to handle the **Celery worker** and the **Main API** separately. This allows you to deploy them as separate services/containers.

### Dockerfile for the Celery Worker

**`Dockerfile_CELERY_WORKER`**:

```dockerfile
# -----------------------------------------------------------------------------
# 1) Start from your Python base image
# -----------------------------------------------------------------------------
FROM python:3.13.1-slim

# -----------------------------------------------------------------------------
# 2) Install system dependencies required by Node.js, Playwright, etc.
# -----------------------------------------------------------------------------
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    ca-certificates \
    fonts-liberation \
    libappindicator3-1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libgdk-pixbuf2.0-0 \
    libnspr4 \
    libnss3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    xdg-utils \
    libgbm-dev \
    libxshmfence-dev \
    && rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------------
# 3) Install Node.js (example: Node 20)
# -----------------------------------------------------------------------------
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs

# -----------------------------------------------------------------------------
# 4) Install pnpm globally
# -----------------------------------------------------------------------------
RUN npm install -g pnpm

# -----------------------------------------------------------------------------
# 5) Install Node application dependencies (Playwright, etc.)
# -----------------------------------------------------------------------------
WORKDIR /usr/src/app

COPY src/package*.json src/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

# Install Playwright browser(s)
RUN npx playwright install chromium

# Copy the rest of your Node/JS code
COPY src/ ./src/

# (Optional) Expose a port if needed (e.g. if Node were to run a service)
EXPOSE 37563

# -----------------------------------------------------------------------------
# 6) Install Python application dependencies (Celery, etc.)
# -----------------------------------------------------------------------------
WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/
WORKDIR /app/src

# -----------------------------------------------------------------------------
# 7) Default CMD: Start the Celery worker
# -----------------------------------------------------------------------------
CMD ["celery", "-A", "celery_task.celery_app", "worker", "--loglevel=info", "--concurrency=8"]
```

### Dockerfile for the Main API

**`Dockerfile_MAIN_API`**:

```dockerfile
FROM python:3.13.1-slim

WORKDIR /app

COPY src/ /app/src/
COPY requirements.txt /app/

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 31337

WORKDIR /app/src

CMD ["uvicorn", "main_api:app", "--host", "0.0.0.0", "--port", "31337"]
```

### Deploying with Fly.io using Dockerfiles

We also provide two helper scripts for deploying to Fly.io, each of which copies the relevant Dockerfile into place and then invokes `fly deploy` with the correct `.toml` configuration.

1. **`deploy_celery_worker.sh`**:
   ```bash
   #!/bin/bash
   # -*- coding: utf-8 -*-

   rm Dockerfile
   cp Dockerfile_CELERY_WORKER Dockerfile

   fly deploy -c fly_celery_worker.toml
   ```

2. **`deploy_main_api.sh`**:
   ```bash
   #!/bin/bash
   # -*- coding: utf-8 -*-

   rm Dockerfile
   cp Dockerfile_MAIN_API Dockerfile

   fly deploy -c fly_main_api.toml
   ```

- **`fly_celery_worker.toml`** is your Fly configuration for the Celery worker:
  ```toml
  # fly_celery_worker.toml
  app = "webscreencap-celery-worker"
  primary_region = "eze"

  [build]

  [[vm]]
    memory = "1gb"
    cpu_kind = "shared"
    cpus = 1
  ```
- **`fly_main_api.toml`** is your Fly configuration for the main API:
  ```toml
  # fly_main_api.toml
  app = "webscreencap"
  primary_region = "eze"

  [build]

  [http_service]
    internal_port = 31337
    force_https = true
    auto_stop_machines = "stop"
    auto_start_machines = true
    min_machines_running = 0
    processes = ["app"]

  [[vm]]
    size = "shared-cpu-1x"
  ```

#### Setting Environment Variables as Fly Secrets

When deploying to Fly.io, you’ll want to ensure your environment variables (e.g., `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`) are securely set. You can do this using Fly secrets. For instance, if you have your secrets already exported locally:

```bash
export AWS_ACCESS_KEY_ID="..."
export AWS_ENDPOINT_URL_S3="..."
export AWS_REGION="..."
export AWS_SECRET_ACCESS_KEY="..."
export BUCKET_NAME="..."
export API_KEY="..."
```

Then you can run:

```bash
fly secrets set \
  AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
  AWS_ENDPOINT_URL_S3="$AWS_ENDPOINT_URL_S3" \
  AWS_REGION="$AWS_REGION" \
  AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
  BUCKET_NAME="$BUCKET_NAME" \
  API_KEY="$API_KEY"
```

Fly will store these as secrets and inject them into your application’s environment at runtime. Make sure to set them for **both** the Celery worker app and the main API app, if you’re running them separately.

---

## Contributing

1. **Fork** the repository  
2. **Create** a feature branch  
3. **Commit** your changes  
4. **Push** the branch  
5. **Create** a Pull Request  

We appreciate PRs for improvements, bug fixes, and new features.

---

## License

This project is licensed under the [MIT License](https://opensource.org/licenses/MIT). Feel free to use and modify as needed.
