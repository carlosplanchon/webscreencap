# WebScreenCAP

[![Tests](https://github.com/carlosplanchon/webscreencap/actions/workflows/tests.yml/badge.svg)](https://github.com/carlosplanchon/webscreencap/actions/workflows/tests.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/carlosplanchon/webscreencap)

A microservice that captures screenshots and HTML content from URLs asynchronously using **FastAPI** and **TaskIQ**, leveraging **Node.js** with **Patchright** (a Playwright patched to avoid bot detection) and **Google Chrome** behind the scenes. Captured artifacts (screenshots, HTML) are uploaded to an S3-compatible storage (e.g., Tigris, MinIO, AWS S3).

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
- [Running the Worker](#running-the-worker)
- [Local Development](#local-development)
  - [Running Everything with Docker Compose](#running-everything-with-docker-compose)
  - [Installing Dependencies with `dependencies.sh`](#installing-dependencies-with-dependenciessh)
  - [Python Dependencies](#python-dependencies)
- [Tests](#tests)
- [Dockerization](#dockerization)
  - [Dockerfile for the Worker](#dockerfile-for-the-worker)
  - [Dockerfile for the Main API](#dockerfile-for-the-main-api)
  - [Deploying with Fly.io using Dockerfiles](#deploying-with-flyio-using-dockerfiles)
    - [Setting Environment Variables as Fly Secrets](#setting-environment-variables-as-fly-secrets)
- [Contributing](#contributing)
- [License](#license)

---

## Features

1. **Async Screenshot & HTML Capture**  
   Offload screenshot tasks to a TaskIQ worker for asynchronous processing.

2. **Undetected Browsing**  
   Uses [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-nodejs), a Playwright patched to avoid bot detection, with Google Chrome in a real window, on a virtual display (Xvfb).

3. **Ad-blocking**  
   Integrates [@ghostery/adblocker-playwright](https://www.npmjs.com/package/@ghostery/adblocker-playwright) for blocking ads and trackers while capturing pages. Its styles hide ad slots in the screenshot, but they're left out of the saved HTML. The filter lists are downloaded and parsed when the worker image is built (the build fails if any of them can't be downloaded). The worker downloads them again when it starts, unless it did less than a day ago, and then once a day: captures switch to the new lists once they're complete, and if a download fails or takes more than 10 minutes, they keep the ones there were.

4. **S3-Compatible Upload**  
   Captured artifacts are uploaded to Tigris (or any S3-compatible storage configured via environment variables).

5. **API Key Validation**  
   Endpoints require a valid `API_KEY`, sent in the `X-API-Key` header.

6. **SSRF Protection**  
   The browser can only reach public addresses: URLs, redirects and anything a page loads that point to localhost, private networks or cloud metadata endpoints are blocked. Nothing skips the proxy that enforces it: Chrome runs without QUIC, it can't resolve host names on its own, and WebRTC can't send UDP, because of a Chrome policy that the worker image installs (captures fail without it, see [Installation](#installation)).

7. **Sandboxed Browser**  
   The worker runs as a non-root user, so Chrome keeps its sandbox (see [Dockerization](#dockerization)).

8. **REST API**  
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
│  TaskIQ Worker                                       │
│ capture_screenshot_and_html → Invokes Node.js script │
└───────┬──────────────────────────────────────────────┘
        │
┌───────┴──────────┐
│   Node.js        │
│   (scraper.js)   │
│ Patchright,      │
│ Google Chrome    │
└───────┬──────────┘
        │
        ▼
  S3-Compatible Object Storage
```

1. A user sends a `POST` request to the FastAPI endpoint, with the `url` in a JSON body and the API key in the `X-API-Key` header.
2. FastAPI validates the API key and the URL (only `http`/`https`), then queues a TaskIQ task in Redis.
3. The TaskIQ worker runs the Node.js script (`scraper.js`) to:
    - Launch Google Chrome with ad-block, behind a local proxy (`egress_proxy.js`) that only lets it reach public addresses.
    - Capture a full-page screenshot (unless the request leaves it out) & HTML, or, for a URL that isn't a page (a PDF, an image, a download...), the file as the site sent it.
    - Upload both artifacts to S3-compatible storage.
4. The worker stores a result with the file URLs in Redis.
5. The user can retrieve the task status (and result) via another endpoint.

---

## Requirements

- **Python** ≥ 3.11 (uv installs it if it's missing)
- **[uv](https://docs.astral.sh/uv/)** for Python dependencies
- **Node.js** 22 or later (the worker image uses 24)
- **npm** / **pnpm** / **yarn** for installing Node dependencies
- **Redis** 6.2 or later, for the task queue and the results
- **S3-Compatible Object Storage** (e.g., Tigris, MinIO, AWS S3) credentials
- **Google Chrome**, with the policy that keeps WebRTC behind the egress proxy (see [Installation](#installation)), and a display for its window (on a server, a virtual one: Xvfb)

---

## Installation

1. **Clone the repository**:

   ```bash
   git clone https://github.com/carlosplanchon/webscreencap.git
   cd webscreencap
   ```

2. **Install Python dependencies**:

   ```bash
   uv sync
   ```

   > This creates a `.venv` with the exact versions pinned in `uv.lock` (the dependencies are declared in `pyproject.toml`), including the `dev` group (`fastapi-cli`), which the Docker images leave out.

3. **Install Node.js dependencies**:
   - If you’re on Arch Linux (or derivative), you can leverage the `src/dependencies.sh` script, which installs system dependencies plus Node modules via **pnpm**.
   - Otherwise, ensure you have Node.js and pnpm installed, then run:
     ```bash
     cd src
     pnpm install --frozen-lockfile
     pnpm patchright install chrome
     ```
     `patchright install chrome` installs Google Chrome on Debian and Ubuntu; elsewhere, install Chrome as usual.

4. **Give Chrome the policy that keeps WebRTC behind the egress proxy**, as the worker image does (captures fail without it):

   ```bash
   sudo mkdir -p /etc/opt/chrome/policies/managed
   echo '{ "WebRtcIPHandling": "disable_non_proxied_udp" }' | sudo tee /etc/opt/chrome/policies/managed/webscreencap.json
   ```

   > It applies to every Chrome on the machine, yours included: WebRTC (video calls, for instance) then only uses TCP, unless a proxy supports UDP, and Chrome says it's managed by your organization. The Docker Compose stack doesn't need it (see [Local Development](#local-development)).

5. **Set up or ensure you have a running Redis**.

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
| `REDIS_URL`            | Redis for the task queue and the results                      | `redis://localhost:6379/0`                          |
| `FILE_URL_MODE`        | How the returned file URLs are built: `presigned` (default) or `public` | `presigned`                              |
| `PRESIGNED_URL_EXPIRES_IN` | Lifetime of presigned URLs, in seconds (up to 604800, 7 days) | `86400` (default, 1 day)                 |
| `PUBLIC_FILE_BASE_URL` | Base of the URLs in `public` mode (required there)            | `https://screenshots-bucket.t3.tigrisfiles.io`      |

> **Note**: Both the API and the worker need `REDIS_URL`, and fail at startup if it's missing or Redis doesn't answer.

**File URLs**: by default, the worker returns presigned URLs, which work even if the bucket is private and expire after `PRESIGNED_URL_EXPIRES_IN` seconds (by default 1 day, as long as the task result is kept). If the bucket is public, set `FILE_URL_MODE=public` and `PUBLIC_FILE_BASE_URL` to get permanent URLs (`<PUBLIC_FILE_BASE_URL>/<file>`); on Tigris, public files are served from `https://<bucket>.t3.tigrisfiles.io`. These three variables only matter to the worker.

With presigned URLs, the files are of no use once their URLs expire, but they stay in the bucket. An expiration rule deletes them; for instance, after 2 days (longer than the URLs last, unless you raise `PRESIGNED_URL_EXPIRES_IN`):

```bash
aws s3api put-bucket-lifecycle-configuration --endpoint-url https://t3.storage.dev --bucket <bucket> \
  --lifecycle-configuration '{"Rules": [{"ID": "expire-captures", "Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"Days": 2}}]}'
```

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

### 2. Start the worker

In another terminal, also from `src/`:

```bash
uv run ./worker.sh
```

Where:
- `worker.sh` checks that Redis answers, then starts the TaskIQ worker with the broker in `tasks.py` (`taskiq worker tasks:broker`), one capture at a time.
- Chrome opens its window on your display. Without one, run it on a virtual display with `uv run ./display.sh ./worker.sh`: `display.sh` starts Xvfb on a free display, out of the terminal's reach so that a Ctrl+C still lets the capture finish, and stops it when the worker ends.

Now you have the API and the worker up and running.

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
- `screenshot` (optional, `true` by default): `false` leaves the screenshot out, for when only the HTML (or the file) is wanted. Everything else is captured the same way.

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
  "id": "<task_id>",
  "message": "Task started successfully"
}
```
- `id` is the task identifier.
- If the URL or one of its redirects points to a non-public address (localhost, a private network, a cloud metadata endpoint...), the task fails. Anything else the page loads is held to the same rule and blocked.
- Screenshots are full-page, as Chrome renders the page in a 1920×1080 window, but cut at 8192 px of height, so that very long pages don't run the worker out of memory (`MAX_SCREENSHOT_WIDTH` / `MAX_SCREENSHOT_HEIGHT` in `scraper.js`). A longer page is cut there while it's captured, since Chrome draws all of it for a full-page screenshot, which can take minutes; the saved HTML is the whole page.
- Before the capture, the page is scrolled down to the bottom, one screen at a time (for up to 30 s), and then given up to 3 s to finish loading the scripts, data and images that the scroll asked for. So the HTML has the content that the page loads lazily, even below the screenshot.
- The HTML includes the content of open shadow roots (as in web components), as `<template shadowrootmode="open">` elements inside their hosts; closed shadow roots can't be read.
- The page is captured once it loads: it gets up to 60 s for its HTML to arrive, and then up to 10 s for the rest, so a page whose resources never finish loading is captured anyway, with what it has by then (unless one of them keeps the page from rendering at all, as a stalled script in its `<head>` does: then the task fails). Redirects the page makes by itself (JavaScript, meta refresh) are followed, up to 5, and a page that keeps navigating after that is captured without scrolling; if a redirect leads to an address that can't be loaded, the task fails.
- Windows that the page opens (popups) are closed as soon as they open, and Chrome can't print, so a page that prints itself (`window.print()`) is captured as usual.

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
    "html_url": "https://screenshots-bucket.<S3-endpoint>/page-<timestamp>-<hash>.html?X-Amz-Algorithm=...&X-Amz-Signature=...",
    "file_url": null,
    "content_type": "text/html; charset=utf-8",
    "http_status": 200,
    "final_url": "https://example.com/"
  }
}
```
- `screenshot_url` is a PNG of what Chrome shows (`null` if the request left the screenshot out), and `html_url` the page's HTML, saved as UTF-8 after its scripts ran.
- A URL that Chrome doesn't show as a page (`text/html` or `application/xhtml+xml`), such as a PDF, an image, JSON, text or SVG, has no `html_url`: `file_url` is the file exactly as the site sent it, with its `Content-Type` (or, without one, the type Chrome made out from its content), and `screenshot_url` is how Chrome shows it. Files over 50 MB fail the task. Chrome keeps no copy of PDFs, video, audio or large files, and of text it only keeps what it read, which is the same bytes only for UTF-8, so it downloads those again: a URL that only answers once, or answers something else the second time, or a sandboxed one (`Content-Security-Policy: sandbox`), fails there. A file that doesn't finish loading in 60 s, such as a live stream, is left out, and only its screenshot is saved (without the screenshot, the task fails).
- A URL that Chrome downloads (`Content-Disposition: attachment`, or a type it can't show) has no `screenshot_url` either, and its file keeps the site's `Content-Disposition`, so it downloads with the same name; if it doesn't finish in 60 s, the task fails. When a page navigates by itself to a file that Chrome shows, the file is what's captured; a download that a page starts by itself is stopped and left out, and the page is captured.
- `content_type` is the `Content-Type` the site sent (the last one, if it sent several, as Chrome goes by it), `http_status` its HTTP status, and `final_url` its address after redirects, including the ones the page makes by itself. Error pages (`404`, `500`...) are captured too, so check `http_status`. A response without content (`204`), or an HTTP error sent as a download (which Chrome doesn't save), has no screenshot, HTML or file, and its `message` says so.
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
- Returns `404` if the task id is unknown or its result has already expired (results are kept for 1 day). A task that waits in the queue for more than 1 day (with the worker down, for instance) is dropped, and returns `404` too.

---

## Running the Worker

The worker is defined in **`tasks.py`**, and **`worker.sh`** starts it (run it from `src/`):

```bash
uv run ./worker.sh
```

- It runs `taskiq worker tasks:broker`, the TaskIQ broker in `tasks.py`, which uses a Redis stream. Before that, it fails if `REDIS_URL` is missing or Redis doesn't answer, since TaskIQ would keep restarting a worker process that can't start.
- Make sure your environment variables are set so the Node.js script can access them.
- When it's told to stop (Ctrl+C included), it finishes the capture in progress first.
- If it dies in the middle of a capture, the capture is retried once, about 5 minutes later; if it's cut short again, it fails.

---

## Local Development

For local development:

1. **Clone** this repository.
2. **Install** dependencies as described or use the included scripts.
3. Configure environment variables or set up a `.env` file.  
4. **Run** the FastAPI server and the worker.
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

If you are on an **Arch Linux** (or derivative) system, `src/dependencies.sh` installs the system packages for Node.js and a virtual display (Xvfb), then the Node modules exactly as pinned in `pnpm-lock.yaml`. Google Chrome isn't in Arch's official repositories: install it from the AUR (`google-chrome`).

```bash
sudo pacman -S nodejs npm pnpm --noconfirm --needed
# A virtual display, for machines without one
sudo pacman -S xorg-server-xvfb --noconfirm --needed

pnpm install --frozen-lockfile
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

The `dev` group (`fastapi-cli`) is only for local use: the Docker images install the rest with `uv sync --locked --no-dev`, which fails if `uv.lock` is out of date with `pyproject.toml`.

---

## Tests

`tests/run.sh` runs the test suite in a throwaway Docker Compose stack (`compose.test.yaml`) and removes the stack afterwards; if a test fails, it prints the API and worker logs first:

```bash
tests/run.sh               # everything
tests/run.sh -k redirect   # only the tests whose name matches
```

Besides the service itself, that stack has a "public" test website and an internal service that browsers must never reach. The tests cover:

- **The API**: the `X-API-Key` header, validation (`401`, `404`, `405`, `422`), results (also without the screenshot), the format of failures, that it keeps working after Redis drops its connections (as in a restart), and that its docs are under `/webscreencap` too, with the project's version.
- **The egress proxy**: its address policy, that it passes on the headers as they came (repeated ones included), and that captures need the Chrome policy for WebRTC (`tests/egress_proxy.test.js`, with `node --test`), that pages can't reach internal addresses or the loopback through redirects, iframes, `fetch`, workers, WebSockets, WebRTC and so on, while public addresses still load, that a file Chrome downloads again (a PDF) is held to the same rule, and that a site can't make its capture fail by posing as the proxy.
- **The worker**: that it fails at startup if `REDIS_URL` is missing or Redis doesn't answer, that its display comes back when its container is restarted, that a Ctrl+C lets the capture in progress finish (also on a display of its own, which stops with it), that finished tasks leave the queue and entries that aren't tasks are dropped, that a dead worker's task is taken over without waiting for another one, and that a task that already has a result, or that waited too long, doesn't run again.
- **Captures**: that the browser is Google Chrome with a real window and no signs of automation, pages that never stop making requests, pages with resources that never finish loading, HTML that takes a while to arrive, pages that print themselves or open popups, pages that redirect or reload by themselves, navigations that load no document (`204`, downloads, also through a redirect or a reload of the page), SVG documents, the 1920×8192 px cap (and that a longer page is left as it was, style attributes included), lazy content below it (also with smooth scrolling), the fonts for non-Latin scripts, the HTML's encoding, the content of open shadow roots, the HTTP status and final URL (also of HTTP errors without a body, and after a download), files that aren't pages (saved as sent, also text that isn't UTF-8 or has a byte order mark, and files without a Content-Type; the PDFs, video and large files that Chrome downloads again, also under a strict Content-Security-Policy, and the sandboxed ones, or the ones that change, that fail; downloads by header or by type, without a screenshot, and the ones of HTTP errors, without anything; downloads that stall; downloads that the page starts by itself, which are left out; pages that send themselves to a PDF while another address is still loading; live streams, with only their screenshot, or failing without it; and the 50 MB cap, also on downloads that don't say their size), captures without the screenshot (of a page, which is still scrolled for its lazy content, and of a file), pages sent with two Content-Types, the file URL settings, that the ad blocker's styles are left out of the HTML (also when it keeps adding them) and still hide ads when the capture starts over, that failed downloads of the ad-blocking lists fail, that they're downloaded again only once they're a day old, and that failed or stalled updates keep the lists there were (`tests/adblocker.test.js`), and that Chrome runs without root and with its sandbox.

They only use the standard libraries (`unittest` and `node:test`), so they need no extra dependencies. GitHub Actions runs the same script on pushes to `main`, on pull requests and on demand (`.github/workflows/tests.yml`).

---

## Dockerization

We provide two separate Dockerfiles to handle the **worker** and the **Main API** separately. This allows you to deploy them as separate services/containers.

The worker image runs Google Chrome with a real window: `display.sh` starts a virtual display (Xvfb) for it, on a free display number, and then runs the command. Chrome has no Linux build for ARM, so the image only builds for amd64, and it gets the Chrome version of the day it's built: to update Chrome, rebuild it without the cache (`docker compose build --no-cache worker`).

The worker runs as `pwuser`, so Chrome can keep its sandbox. Docker's default seccomp profile doesn't let it create the user namespaces the sandbox needs, so the worker's containers use `seccomp_profile.json`: Docker's default profile plus the user namespace rule from [Playwright's docs](https://playwright.dev/docs/docker). The compose files already pass it; with `docker run`, add `--security-opt seccomp=seccomp_profile.json`. Fly runs the image as a VM instead of a Docker container, so the profile doesn't apply there.

### Dockerfile for the Worker

**`Dockerfile_WORKER`**:

```dockerfile
# -----------------------------------------------------------------------------
# 1) Start from your Python base image
# -----------------------------------------------------------------------------
FROM python:3.15.1-slim-bookworm

# -----------------------------------------------------------------------------
# 2) Install system dependencies required by Node.js, Chrome, etc.
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
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------------
# 3) Install Node.js 24
# -----------------------------------------------------------------------------
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y nodejs

# -----------------------------------------------------------------------------
# 4) Install pnpm globally
# -----------------------------------------------------------------------------
RUN npm install -g pnpm@12.14.0

# -----------------------------------------------------------------------------
# 5) Install Node application dependencies (Patchright, etc.)
#    NOTE: Since your package.json and pnpm-lock.yaml are in src/, COPY them from there.
# -----------------------------------------------------------------------------
WORKDIR /app/src

# Copy Node dependency manifests from src/
COPY src/package*.json src/pnpm-lock.yaml ./

RUN pnpm install --frozen-lockfile

# Install Google Chrome, which runs with a real window on a virtual display
RUN npx patchright install chrome \
    && mkdir -p /tmp/.X11-unix \
    && chmod 1777 /tmp/.X11-unix \
    && rm -rf /var/lib/apt/lists/*

# Keeps WebRTC from sending UDP outside the proxy
RUN mkdir -p /etc/opt/chrome/policies/managed \
    && echo '{ "WebRtcIPHandling": "disable_non_proxied_udp" }' > /etc/opt/chrome/policies/managed/webscreencap.json

# -----------------------------------------------------------------------------
# 6) Install Python application dependencies (TaskIQ, etc.)
#    Exact versions from uv.lock, without the dev group.
# -----------------------------------------------------------------------------
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/
WORKDIR /app

COPY pyproject.toml uv.lock .python-version /app/
RUN UV_PYTHON_DOWNLOADS=0 uv sync --locked --no-dev --no-cache --compile-bytecode
ENV PATH="/app/.venv/bin:$PATH"

COPY src/ /app/src/
WORKDIR /app/src

# Build the ad-blocking cache
RUN node adblocker.js

# -----------------------------------------------------------------------------
# 7) Default CMD: Start the TaskIQ worker
#    (You can override this later if you want to run `node` instead)
#    One task at a time, so Chrome fits in the VM's memory.
# -----------------------------------------------------------------------------
# Without root, so Chrome runs with its sandbox
RUN useradd --create-home pwuser
USER pwuser
ENTRYPOINT ["/app/src/display.sh"]
CMD ["./worker.sh"]
```

### Dockerfile for the Main API

**`Dockerfile_MAIN_API`**:

```dockerfile
FROM python:3.15.1-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

WORKDIR /app

# Exact versions from uv.lock, without the dev group
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
fly deploy -c fly_worker.toml
fly deploy -c fly_main_api.toml
```

- **`fly_worker.toml`** is your Fly configuration for the worker. Fly waits `kill_timeout` seconds before killing a machine it stops (on deploys, for instance). The default of 5 s would lose the capture in progress, so the worker gets 200 s, longer than a capture can take. Chrome can take more than 1 GB on heavy pages, so the VM has 2 GB, the most for a shared CPU:
  ```toml
  app = 'webscreencap-taskiq-worker'
  primary_region = 'iad'
  kill_timeout = 200

  [build]
    dockerfile = 'Dockerfile_WORKER'

  [[vm]]
    memory = '2gb'
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
export REDIS_URL="..."
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
  REDIS_URL="$REDIS_URL"
```

Fly will store these as secrets and inject them into your application’s environment at runtime. Run the same command with `-a webscreencap-taskiq-worker`, since **both** the worker app and the main API app need them. Setting secrets restarts the app’s Machines; add `--stage` to apply them on the next deploy instead.

The file URL settings aren't secret, so they can go in an `[env]` section of `fly_worker.toml` instead (only needed to change the defaults):

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
