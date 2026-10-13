// Ad and tracker blocking with Ghostery's prebuilt lists. The parsed engine is
// cached on disk: the worker image builds the cache (`node adblocker.js`, see
// Dockerfile_CELERY_WORKER), so captures don't download the lists every time.
// If the cache is missing or can't be loaded, the lists are fetched and it's rewritten.

const fs = require("fs/promises");
const path = require("path");
const { PlaywrightBlocker } = require("@ghostery/adblocker-playwright");
const { fetch } = require("cross-fetch");

const CACHE_PATH = path.join(__dirname, "adblocker-engine.bin");

function loadBlocker() {
    return PlaywrightBlocker.fromPrebuiltAdsAndTracking(fetch, {
        path: CACHE_PATH,
        read: fs.readFile,
        write: fs.writeFile,
    });
}

if (require.main === module) {
    loadBlocker().then(() => console.log(`Adblocker engine cached at ${CACHE_PATH}`));
}

module.exports = { loadBlocker };
