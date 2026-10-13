#!/bin/bash
# Local setup on Arch Linux: system packages for Node.js and Playwright's
# Chromium, then the Node dependencies exactly as pinned in pnpm-lock.yaml.
cd "$(dirname "$0")"

sudo pacman -S nodejs npm pnpm --noconfirm --needed

# Required by Playwright's Chromium.
sudo pacman -S atk at-spi2-atk --noconfirm --needed
sudo pacman -S libcups libxkbcommon libxcomposite --noconfirm --needed
sudo pacman -S alsa-lib --noconfirm --needed

pnpm install --frozen-lockfile
pnpm playwright install chromium
