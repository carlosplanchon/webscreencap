// The parsed lists are cached on disk; the worker image builds the cache

const fs = require("fs/promises");
const path = require("path");
const { PlaywrightBlocker } = require("@ghostery/adblocker-playwright");
const { fetch } = require("cross-fetch");

const CACHE_PATH = path.join(__dirname, "adblocker-engine.bin");
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

// Runs in the page
function removeBlockerStyles(marker) {
    for (const style of document.querySelectorAll("style")) {
        if (style.textContent.startsWith(marker)) {
            style.remove();
        }
    }
}

if (require.main === module) {
    loadBlocker().then(() => console.log(`Adblocker engine cached at ${CACHE_PATH}`));
}

module.exports = { STYLE_MARKER, loadBlocker, removeBlockerStyles };
