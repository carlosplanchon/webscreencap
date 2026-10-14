# WebScreenCAP

[![Tests](https://github.com/carlosplanchon/webscreencap/actions/workflows/tests.yml/badge.svg)](https://github.com/carlosplanchon/webscreencap/actions/workflows/tests.yml)

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
  - [Running Everything with Docker Compose](#running-everything-with-docker-compose)
  - [Installing Dependencies with `dependencies.sh`](#installing-dependencies-with-dependenciessh)
  - [Python Dependencies](#python-dependencies)
- [Tests](#tests)
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
   Integrates [@ghostery/adblocker-playwright](https://www.npmjs.com/package/@ghostery/adblocker-playwright) for blocking ads and trackers while capturing pages. The filter lists are downloaded and parsed when the worker image is built.

4. **S3-Compatible Upload**  
   Captured artifacts are uploaded to Tigris (or any S3-compatible storage configured via environment variables).

5. **API Key Validation**  
   Endpoints require a valid `API_KEY`, sent in the `X-API-Key` header.

6. **SSRF Protection**  
   The browser can only reach public addresses: URLs, redirects and anything a page loads that point to localhost, private networks or cloud metadata endpoints are blocked.

7. **REST API**  
   Built with **FastAPI**, offering straightforward endpoints to initiate and poll screenshot tasks.

### DeepWiki Docs: [https://deepwiki.com/carlosplanchon/webscreencap](https://deepwiki.com/carlosplanchon/webscreencap)

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
  S3-Compatible Object Storage
```

1. A user sends a `POST` request to the FastAPI endpoint, with the `url` in a JSON body and the API key in the `X-API-Key` header.
2. FastAPI validates the API key and the URL (only `http`/`https`), then dispatches a Celery task.
3. The Celery worker runs the Node.js script (`scraper.js`) to:
    - Launch a headless Chromium with stealth & ad-block, behind a local proxy (`egress_proxy.js`) that only lets it reach public addresses.
    - Capture a full-page screenshot & HTML.
    - Upload both artifacts to S3-compatible storage.
4. Celery returns a result object containing the S3 URLs.
5. The user can retrieve the task status (and result) via another endpoint.

---

## Requirements

- **Python** ≥ 3.13 (uv installs it if it's missing)
- **[uv](https://docs.astral.sh/uv/)** for Python dependencies
- **Node.js** 22 or later (the worker image uses 24)
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
   uv sync
   ```

   > This creates a `.venv` with the exact versions pinned in `uv.lock` (the dependencies are declared in `pyproject.toml`), including the `dev` group (`fastapi-cli`, `flower`), which the Docker images leave out.

3. **Install Node.js dependencies**:
   - If you’re on Arch Linux (or derivative), you can leverage the `src/dependencies.sh` script, which installs system dependencies plus Node modules via **pnpm**.
   - Otherwise, ensure you have Node.js and pnpm installed, then run:
     ```bash
     cd src
     pnpm install --frozen-lockfile
     pnpm playwright install chromium
     ```

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
| `CELERY_BROKER_URL`    | Celery broker URL                                             | `redis://localhost:6379/0`                          |
| `CELERY_RESULT_BACKEND`| Celery result backend                                         | `redis://localhost:6379/0`                          |
| `FILE_URL_MODE`        | How the returned file URLs are built: `presigned` (default) or `public` | `presigned`                              |
| `PRESIGNED_URL_EXPIRES_IN` | Lifetime of presigned URLs, in seconds (up to 604800, 7 days) | `86400` (default, 1 day)                 |
| `PUBLIC_FILE_BASE_URL` | Base of the URLs in `public` mode (required there)            | `https://screenshots-bucket.t3.tigrisfiles.io`      |

> **Note**: Both the API and the Celery worker need `CELERY_BROKER_URL` and `CELERY_RESULT_BACKEND`, and fail at startup if either is missing.

**File URLs**: by default, the worker returns presigned URLs, which work even if the bucket is private and expire after `PRESIGNED_URL_EXPIRES_IN` seconds (by default 1 day, as long as Celery keeps the task result). If the bucket is public, set `FILE_URL_MODE=public` and `PUBLIC_FILE_BASE_URL` to get permanent URLs (`<PUBLIC_FILE_BASE_URL>/<file>`); on Tigris, public files are served from `https://<bucket>.t3.tigrisfiles.io`. These three variables only matter to the worker.

---

## Usage

### 1. Start the FastAPI service

From the `src/` directory, with uvicorn:

```bash
uv run uvicorn main_api:app --host 0.0.0.0 --port 31337
```

- `main_api:app` references your FastAPI application file/module and the `app` object within it.
- The example config in the code uses `if __name__ == "__main__": uvicorn.run(app, ... )` to start the server.  
- Adjust host/port as necessary.

### 2. Start the Celery worker

In another terminal, also from `src/`:

```bash
uv run celery -A celery_task.celery_app worker --loglevel=info
```

Where:
- `-A celery_task.celery_app` matches the Celery app instance in your `celery_task.py`.

Now you have the API and the Celery worker up and running.

---

## API Reference

Below are the primary endpoints. All endpoints require the `X-API-Key` header to match your environment’s `API_KEY`; otherwise they return `401`.

### 1. Initiate Screenshot Capture

**Endpoint**:  
```
POST /api/task/screenshot
```

**Body** (JSON):
- `url`: The target URL to capture. Only `http` and `https` URLs are accepted; anything else returns `422`.

**Example**:
```bash
curl -X POST "http://127.0.0.1:31337/webscreencap/api/task/screenshot" \
  -H "X-API-Key: mysecretkey" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
```

**Response** (JSON):
```json
{
  "id": "<celery_task_id>",
  "message": "Task started successfully"
}
```
- `id` is the unique Celery task identifier.
- If the URL points to a non-public address (localhost, a private network, a cloud metadata endpoint...), the task fails. Redirects and anything the page loads are held to the same rule.
- Screenshots are full-page but cut at 1920×8192 px, so that very long pages don't run the worker out of memory (`MAX_SCREENSHOT_WIDTH` / `MAX_SCREENSHOT_HEIGHT` in `scraper.js`).

### 2. Get Task Details

**Endpoint**:  
```
GET /api/task_details/{task_id}
```

**Path Parameters**:
- `task_id`: The task ID from the previous step.

**Example**:
```bash
curl "http://127.0.0.1:31337/webscreencap/api/task_details/abc123" \
  -H "X-API-Key: mysecretkey"
```

**Response** (JSON):
```json
{
  "task_id": "abc123",
  "status": "SUCCESS",
  "result": {
    "status": "success",
    "message": "Screenshot and HTML processed successfully",
    "screenshot_url": "https://screenshots-bucket.<S3-endpoint>/screenshot-<timestamp>-<hash>.png?X-Amz-Algorithm=...&X-Amz-Signature=...",
    "html_url": "https://screenshots-bucket.<S3-endpoint>/page-<timestamp>-<hash>.html?X-Amz-Algorithm=...&X-Amz-Signature=..."
  }
}
```
- While the task waits or runs, `status` is `PENDING` and `result` is `null`.
- If the capture fails, `status` is `FAILURE` and `result.message` says why; the details stay in the worker logs:
  ```json
  {
    "task_id": "abc123",
    "status": "FAILURE",
    "result": {
      "status": "error",
      "message": "page.goto: net::ERR_CERT_DATE_INVALID at https://expired.badssl.com/"
    }
  }
  ```
- Returns `404` if the task id is unknown or its result has already expired (Celery keeps results for 1 day by default).

---

## Running the Celery Worker

The Celery worker is defined in **`celery_task.py`** (run it from `src/`):

```bash
uv run celery -A celery_task.celery_app worker --loglevel=info
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

### Running Everything with Docker Compose

`compose.yaml` starts the whole service locally: the API, the worker, Redis, and a local S3 ([moto](https://github.com/getmoto/moto)) instead of Tigris.

```bash
docker compose up --build
```

The API answers on `http://127.0.0.1:31337/webscreencap/api`, with the API key `local-dev-key`:

```bash
curl -X POST "http://127.0.0.1:31337/webscreencap/api/task/screenshot" \
  -H "X-API-Key: local-dev-key" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
```

The captured files stay in moto's memory, and their URLs point to `10.250.250.10:5000`, which a Linux host can reach directly.

### Installing Dependencies with `dependencies.sh`

If you are on an **Arch Linux** (or derivative) system, `src/dependencies.sh` installs the system packages for Node.js and Playwright's Chromium, then the Node modules exactly as pinned in `pnpm-lock.yaml`:

```bash
sudo pacman -S nodejs npm pnpm --noconfirm --needed
# Required by Playwright's Chromium.
sudo pacman -S atk at-spi2-atk libcups libxkbcommon libxcomposite alsa-lib --noconfirm --needed

pnpm install --frozen-lockfile
pnpm playwright install chromium
```

You can adapt this script for other package managers / distributions as needed.

### Python Dependencies

Python dependencies are declared in `pyproject.toml` and pinned in `uv.lock`, both managed with [uv](https://docs.astral.sh/uv/):

```bash
uv sync                  # install everything, including the dev group
uv add <package>         # add a dependency (updates pyproject.toml and uv.lock)
uv add --dev <package>   # add a tool that's only needed locally
uv lock --upgrade        # upgrade the pinned versions
```

The `dev` group (`fastapi-cli`, `flower`) is only for local use: the Docker images install the rest with `uv sync --locked --no-dev`, which fails if `uv.lock` is out of date with `pyproject.toml`.

---

## Tests

`tests/run.sh` runs the test suite in a throwaway Docker Compose stack (`compose.test.yaml`) and removes the stack afterwards:

```bash
tests/run.sh               # everything
tests/run.sh -k redirect   # only the tests whose name matches
```

Besides the service itself, that stack has a "public" test website and an internal service that browsers must never reach. The tests cover:

- **The API**: the `X-API-Key` header, validation (`401`, `404`, `405`, `422`), results and the format of failures.
- **The egress proxy**: its address policy (`tests/egress_proxy.test.js`, with `node --test`), and that pages can't reach internal addresses or the loopback through redirects, iframes, `fetch`, workers, WebSockets and so on, while public addresses still load.
- **Captures**: pages that never stop making requests, the 1920×8192 px cap, the fonts for non-Latin scripts and the file URL settings.

They only use the standard libraries (`unittest` and `node:test`), so they need no extra dependencies. GitHub Actions runs the same script on pushes to `main`, on pull requests and on demand (`.github/workflows/tests.yml`).

---

## Dockerization

We provide two separate Dockerfiles to handle the **Celery worker** and the **Main API** separately. This allows you to deploy them as separate services/containers.

### Dockerfile for the Celery Worker

**`Dockerfile_CELERY_WORKER`**:

```dockerfile
# -----------------------------------------------------------------------------
# 1) Start from your Python base image
# -----------------------------------------------------------------------------
FROM python:3.13.16-slim-bookworm

# -----------------------------------------------------------------------------
# 2) Install system dependencies required by Node.js, Playwright, etc.
#    The Noto fonts cover what Liberation doesn't (CJK, Arabic, Devanagari,
#    Thai, emoji...), which otherwise shows up as boxes in the screenshots.
# -----------------------------------------------------------------------------
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    ca-certificates \
    fonts-liberation \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    fonts-noto-core \
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
# 3) Install Node.js 24 (LTS; Playwright needs 22 or later)
# -----------------------------------------------------------------------------
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y nodejs

# -----------------------------------------------------------------------------
# 4) Install pnpm globally (pinned, so builds don't pick up a new major by surprise)
# -----------------------------------------------------------------------------
RUN npm install -g pnpm@12.11.2

# -----------------------------------------------------------------------------
# 5) Install Node application dependencies (Playwright, etc.)
#    They go in /app/src, next to scraper.js: Celery runs /app/src/scraper.js and
#    Node resolves modules relative to the script's directory.
# -----------------------------------------------------------------------------
WORKDIR /app/src

COPY src/package*.json src/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

# Install Playwright's browser: only Chromium's headless shell, since scraper.js
# always runs headless (the full Chromium is only needed for headed runs)
RUN npx playwright install --only-shell chromium

# -----------------------------------------------------------------------------
# 6) Install Python application dependencies (Celery, etc.)
#    Exact versions from uv.lock (fails if the lock is outdated), without the
#    dev group.
# -----------------------------------------------------------------------------
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /uvx /bin/
WORKDIR /app

COPY pyproject.toml uv.lock .python-version /app/
RUN UV_PYTHON_DOWNLOADS=0 uv sync --locked --no-dev --no-cache --compile-bytecode
ENV PATH="/app/.venv/bin:$PATH"

COPY src/ /app/src/
WORKDIR /app/src

# Download and parse the ad-blocking lists once, here, instead of in every capture
RUN node adblocker.js

# -----------------------------------------------------------------------------
# 7) Default CMD: Start the Celery worker
#    One task at a time: each capture's Chromium takes ~500-700 MB, and in tests
#    with the 1 GB of fly_celery_worker.toml two heavy pages at once hit the limit.
# -----------------------------------------------------------------------------
CMD ["celery", "-A", "celery_task.celery_app", "worker", "--loglevel=info", "--concurrency=1"]
```

### Dockerfile for the Main API

**`Dockerfile_MAIN_API`**:

```dockerfile
FROM python:3.13.16-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /uvx /bin/

WORKDIR /app

# Exact versions from uv.lock (fails if the lock is outdated), without the dev group
COPY pyproject.toml uv.lock .python-version /app/
RUN UV_PYTHON_DOWNLOADS=0 uv sync --locked --no-dev --no-cache --compile-bytecode
ENV PATH="/app/.venv/bin:$PATH"

COPY src/ /app/src/

EXPOSE 31337

WORKDIR /app/src

CMD ["uvicorn", "main_api:app", "--host", "0.0.0.0", "--port", "31337"]
```

### Deploying with Fly.io using Dockerfiles

Each app has its own Fly configuration, which points to its Dockerfile with `[build] dockerfile`. Deploy them from the repository root:

```bash
fly deploy -c fly_celery_worker.toml
fly deploy -c fly_main_api.toml
```

- **`fly_celery_worker.toml`** is your Fly configuration for the Celery worker:
  ```toml
  app = 'webscreencap-celery-worker'
  primary_region = 'iad'

  [build]
    dockerfile = 'Dockerfile_CELERY_WORKER'

  [[vm]]
    memory = '1gb'
    cpu_kind = 'shared'
    cpus = 1
  ```
- **`fly_main_api.toml`** is your Fly configuration for the main API:
  ```toml
  app = 'webscreencap'
  primary_region = 'iad'

  [build]
    dockerfile = 'Dockerfile_MAIN_API'

  [http_service]
    internal_port = 31337
    force_https = true
    auto_stop_machines = 'stop'
    auto_start_machines = true
    min_machines_running = 0
    processes = ['app']

  [[vm]]
    size = 'shared-cpu-1x'
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
export CELERY_BROKER_URL="..."
export CELERY_RESULT_BACKEND="..."
```

Then you can run:

```bash
fly secrets set -a webscreencap \
  AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
  AWS_ENDPOINT_URL_S3="$AWS_ENDPOINT_URL_S3" \
  AWS_REGION="$AWS_REGION" \
  AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
  BUCKET_NAME="$BUCKET_NAME" \
  API_KEY="$API_KEY" \
  CELERY_BROKER_URL="$CELERY_BROKER_URL" \
  CELERY_RESULT_BACKEND="$CELERY_RESULT_BACKEND"
```

Fly will store these as secrets and inject them into your application’s environment at runtime. Run the same command with `-a webscreencap-celery-worker`, since **both** the Celery worker app and the main API app need them. Setting secrets restarts the app’s Machines; add `--stage` to apply them on the next deploy instead.

The file URL settings aren't secret, so they can go in an `[env]` section of `fly_celery_worker.toml` instead (only needed to change the defaults):

```toml
[env]
  FILE_URL_MODE = 'public'
  PUBLIC_FILE_BASE_URL = 'https://<bucket>.t3.tigrisfiles.io'
```

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
