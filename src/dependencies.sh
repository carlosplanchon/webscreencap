#!/bin/bash
# Local setup on Arch Linux
cd "$(dirname "$0")"

sudo pacman -S nodejs npm pnpm --noconfirm --needed
# A virtual display, for machines without one
sudo pacman -S xorg-server-xvfb --noconfirm --needed

pnpm install --frozen-lockfile
# Google Chrome isn't in the official repositories, but in the AUR
command -v google-chrome-stable > /dev/null || echo "Install Google Chrome from the AUR (google-chrome)."
