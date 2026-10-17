// The parsed lists are cached on disk; the worker image builds the cache

const fs = require("fs/promises");
const os = require("os");
const path = require("path");
const { PlaywrightBlocker } = require("@ghostery/adblocker-playwright");
const { fetch } = require("cross-fetch");

const CACHE_PATH = path.join(__dirname, "adblocker-engine.bin");
// The worker keeps the lists updated here, since the image's are read-only
const UPDATED_PATH = path.join(os.tmpdir(), "webscreencap-adblocker-engine.bin");
// Starts the styles it adds to pages, which hide ads but aren't part of the page
const STYLE_MARKER = "/* webscreencap ad blocker */";

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

async function loadBlocker(fetchImpl = fetch, cachePath = CACHE_PATH) {
    const blocker = await PlaywrightBlocker.fromPrebuiltAdsAndTracking(checkedFetch(fetchImpl), {
        path: cachePath,
        read: fs.readFile,
        write: fs.writeFile,
    });
    blocker.injectStylesIntoFrame = async (frame, styles) => {
        if (styles.length !== 0) {
            await frame.addStyleTag({ content: `${STYLE_MARKER}\n${styles}` });
        }
    };
    return blocker;
}

// The lists the worker updated, or else the image's
async function loadLatestBlocker(updatedPath = UPDATED_PATH) {
    const updated = await fs.access(updatedPath).then(() => true, () => false);
    return loadBlocker(fetch, updated ? updatedPath : CACHE_PATH);
}

// Downloads the lists again; captures only see them once they're complete
async function updateBlocker(fetchImpl = fetch, updatedPath = UPDATED_PATH) {
    const blocker = await PlaywrightBlocker.fromPrebuiltAdsAndTracking(checkedFetch(fetchImpl));
    const partial = `${updatedPath}.${process.pid}`;
    try {
        await fs.writeFile(partial, blocker.serialize());
        await fs.rename(partial, updatedPath);
    } finally {
        await fs.rm(partial, { force: true });
    }
}

// Runs in the page
function removeBlockerStyles(marker) {
    for (const style of document.querySelectorAll("style")) {
        if (style.textContent.startsWith(marker)) {
            style.remove();
        }
    }
}

if (require.main === module && process.argv[2] === "--update") {
    updateBlocker().then(
        () => console.log(`Adblocker lists updated at ${UPDATED_PATH}`),
        (error) => {
            console.error(`Could not update the adblocker lists: ${error.message}`);
            process.exitCode = 1;
        },
    );
} else if (require.main === module) {
    loadBlocker().then(() => console.log(`Adblocker engine cached at ${CACHE_PATH}`));
}

module.exports = { STYLE_MARKER, loadBlocker, loadLatestBlocker, removeBlockerStyles, updateBlocker };
