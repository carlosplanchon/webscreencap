// Ad and tracker blocking with Ghostery's prebuilt lists. The parsed engine is
// cached on disk: the worker image builds the cache (`node adblocker.js`, see
// Dockerfile_CELERY_WORKER), so captures don't download the lists every time.
// If the cache is missing or can't be loaded, the lists are fetched and it's rewritten.

const fs = require("fs/promises");
const path = require("path");
const { PlaywrightBlocker } = require("@ghostery/adblocker-playwright");
const { fetch } = require("cross-fetch");

const CACHE_PATH = path.join(__dirname, "adblocker-engine.bin");

// The adblocker would parse an error page as a filter list
function checkedFetch(fetchImpl) {
    return async (url) => {
        const response = await fetchImpl(url);
        if (!response.ok) {
            throw new Error(`Failed to download ${url}: HTTP ${response.status}`);
        }
        return response;
    };
}

function loadBlocker(fetchImpl = fetch, cachePath = CACHE_PATH) {
    return PlaywrightBlocker.fromPrebuiltAdsAndTracking(checkedFetch(fetchImpl), {
        path: cachePath,
        read: fs.readFile,
        write: fs.writeFile,
    });
}

if (require.main === module) {
    loadBlocker().then(() => console.log(`Adblocker engine cached at ${CACHE_PATH}`));
}

module.exports = { loadBlocker };
