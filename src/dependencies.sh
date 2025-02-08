sudo pacman -S nodejs npm pnpm --noconfirm --needed

# Required by Puppeteer.
sudo pacman -S atk at-spi2-atk --noconfirm --needed
sudo pacman -S libcups libxkbcommon libxcomposite --noconfirm --needed
sudo pacman -S alsa-lib --noconfirm --needed

pnpm install playwright
pnpm install express
pnpm install playwright-extra
pnpm install puppeteer-extra-plugin-stealth

pnpm install cross-fetch
pnpm install @ghostery/adblocker-playwright

pnpm install valid-url

pnpm playwright install chromium

pnpm install @aws-sdk/client-s3

pnpm install random-useragent
