// The parsed lists are cached on disk; the worker image builds the cache

const fs = require("fs/promises");
const os = require("os");
const path = require("path");
const { PlaywrightBlocker } = require("@ghostery/adblocker-playwright");
const { fetch } = require("cross-fetch");

const CACHE_PATH = path.join(__dirname, "adblocker-engine.bin");
// The worker keeps the lists updated in the user's own cache, since the image's are read-only
const UPDATED_PATH = path.join(os.homedir(), ".cache", "webscreencap", "adblocker-engine.bin");
// The worker downloads the lists again once they're this old
const UPDATE_INTERVAL_MS = 24 * 60 * 60 * 1000;
// A list that stops arriving would hold the update forever
const UPDATE_TIMEOUT_MS = 10 * 60 * 1000;
// Starts the styles it adds to pages, which hide ads but aren't part of the page
const STYLE_MARKER = "/* webscreencap ad blocker */";

// The adblocker would parse an error page as a filter list
function checkedFetch(fetchImpl, signal) {
    return async (url) => {
        const response = await fetchImpl(url, { signal });
        if (!response.ok) {
            throw new Error(`Failed to download ${url}: HTTP ${response.status}`);
        }
        return response;
    };
}

// The frame, with its $$eval done through evaluate, since Patchright's $$eval also goes through the page's world
function isolated(frame) {
    return new Proxy(frame, {
        get(target, property) {
            if (property === "$$eval") {
                return (selector, pageFunction) => target.evaluate(
                    `(${pageFunction})(Array.from(document.querySelectorAll(${JSON.stringify(selector)})))`,
                );
            }
            const value = Reflect.get(target, property);
            return typeof value === "function" ? value.bind(target) : value;
        },
    });
}

function addStyle(content) {
    const style = document.createElement("style");
    style.textContent = content;
    (document.head || document.documentElement).append(style);
}

// It runs in the page's world all the same, and leaves no element behind
function addScript(content) {
    const script = document.createElement("script");
    script.textContent = content;
    (document.head || document.documentElement).append(script);
    script.remove();
}

async function loadBlocker(fetchImpl = fetch, cachePath = CACHE_PATH) {
    const blocker = await PlaywrightBlocker.fromPrebuiltAdsAndTracking(checkedFetch(fetchImpl), {
        path: cachePath,
        read: fs.readFile,
        write: fs.writeFile,
    });
    // All from Patchright's isolated world: going through the page's world while the tab turns into a PDF can freeze it
    const onFrame = blocker.onFrame;
    blocker.onFrame = (frame) => onFrame(isolated(frame));
    blocker.injectStylesIntoFrame = async (frame, styles) => {
        if (styles.length !== 0) {
            await frame.evaluate(addStyle, `${STYLE_MARKER}\n${styles}`);
        }
    };
    // Each in a block of its own, as the blocker does, so that its errors and declarations stay there
    blocker.injectScriptletsIntoFrame = async (frame, scripts) => {
        await Promise.all(scripts.map((script) => frame.evaluate(addScript, `try{${script}}catch(e){}`)));
    };
    return blocker;
}

// The lists the worker updated, or else the image's
async function loadLatestBlocker(updatedPath = UPDATED_PATH) {
    const updated = await fs.access(updatedPath).then(() => true, () => false);
    return loadBlocker(fetch, updated ? updatedPath : CACHE_PATH);
}

// Downloads the lists again, unless that was done less than a day ago; captures only see them once they're complete
async function updateBlocker(fetchImpl = fetch, updatedPath = UPDATED_PATH, timeoutMs = UPDATE_TIMEOUT_MS) {
    const updated = await fs.stat(updatedPath).then((stats) => stats.mtimeMs, () => 0);
    if (Date.now() - updated < UPDATE_INTERVAL_MS) {
        return false;
    }
    const signal = AbortSignal.timeout(timeoutMs);
    const blocker = await PlaywrightBlocker.fromPrebuiltAdsAndTracking(checkedFetch(fetchImpl, signal));
    await fs.mkdir(path.dirname(updatedPath), { recursive: true });
    const partial = `${updatedPath}.${process.pid}`;
    try {
        await fs.writeFile(partial, blocker.serialize());
        await fs.rename(partial, updatedPath);
    } finally {
        await fs.rm(partial, { force: true });
    }
    return true;
}

if (require.main === module && process.argv[2] === "--update") {
    updateBlocker().then(
        (updated) => updated && console.log(`Adblocker lists updated at ${UPDATED_PATH}`),
        (error) => {
            console.error(`Could not update the adblocker lists: ${error.message}`);
            process.exitCode = 1;
        },
    );
} else if (require.main === module) {
    loadBlocker().then(() => console.log(`Adblocker engine cached at ${CACHE_PATH}`));
}

module.exports = { STYLE_MARKER, loadBlocker, loadLatestBlocker, updateBlocker };
