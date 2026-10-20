const { chromium } = require("patchright");
const { S3Client, GetObjectCommand, PutObjectCommand } = require("@aws-sdk/client-s3");
const { getSignedUrl } = require("@aws-sdk/s3-request-presigner");
const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");
const util = require("util");
const { STYLE_MARKER, loadLatestBlocker } = require("./adblocker");
const { assertPublicUrl, assertWebRtcPolicy, startEgressProxy } = require("./egress_proxy");

const NAVIGATION_TIMEOUT_MS = 60000;
const HTTP_ERROR_WITHOUT_BODY = "net::ERR_HTTP_RESPONSE_CODE_FAILURE";
// After this, the page is captured even if some resource never finished loading
const LOAD_TIMEOUT_MS = 10000;
// Redirects the page makes by itself (JavaScript, meta refresh) that are followed
const MAX_NAVIGATIONS = 5;
// Longest wait for the network to go quiet after "load"
const NETWORK_SETTLE_MS = 5000;
const MAX_SCROLL_MS = 30000;
// Longest wait, after scrolling, for the content the scroll made the page load
const LAZY_CONTENT_TIMEOUT_MS = 3000;
const SCRIPT_TIMEOUT_MS = 150000;
// A file that isn't a page (PDF, image, download...) is saved up to this size
const MAX_FILE_BYTES = 50 * 1024 * 1024;
// Longest wait for a file that Chrome shows to finish loading, since a live stream never does
const FILE_TIMEOUT_MS = 60000;
// Raise them only together with the VM's memory
const MAX_SCREENSHOT_WIDTH = 1920;
const MAX_SCREENSHOT_HEIGHT = 8192;

// Configure S3 client
const s3 = new S3Client({
    region: process.env.AWS_REGION,
    endpoint: process.env.AWS_ENDPOINT_URL_S3,
    credentials: {
        accessKeyId: process.env.AWS_ACCESS_KEY_ID,
        secretAccessKey: process.env.AWS_SECRET_ACCESS_KEY
    }
});

async function uploadToTigris(bucketName, fileName, fileContent, contentType, contentDisposition) {
    const params = {
        Bucket: bucketName,
        Key: fileName,
        Body: fileContent,
        ContentType: contentType,
        ContentDisposition: contentDisposition
    };

    try {
        const command = new PutObjectCommand(params);
        await s3.send(command);
    } catch (error) {
        console.error("Error uploading to Tigris:", error);
        throw new Error("Failed to upload file to Tigris Object Storage.");
    }
}

// Presigned file URLs by default, or public ones (FILE_URL_MODE)
function fileUrlBuilder(bucketName) {
    const mode = process.env.FILE_URL_MODE || "presigned";
    if (mode === "public") {
        const base = process.env.PUBLIC_FILE_BASE_URL;
        if (!base) {
            throw new Error("FILE_URL_MODE=public needs PUBLIC_FILE_BASE_URL");
        }
        return async (fileName) => `${base.replace(/\/+$/, "")}/${fileName}`;
    }
    if (mode === "presigned") {
        const expiresIn = Number(process.env.PRESIGNED_URL_EXPIRES_IN || 86400);
        // Presigned URLs (SigV4) can't last more than 7 days
        if (!Number.isInteger(expiresIn) || expiresIn <= 0 || expiresIn > 604800) {
            throw new Error("PRESIGNED_URL_EXPIRES_IN must be a whole number of seconds, up to 604800 (7 days)");
        }
        return (fileName) => getSignedUrl(
            s3, new GetObjectCommand({ Bucket: bucketName, Key: fileName }), { expiresIn }
        );
    }
    throw new Error(`FILE_URL_MODE must be "presigned" or "public", got "${mode}"`);
}

// The message is for API clients; the details only reach the worker logs
function fail(message, details) {
    // process.exit() could cut off what's still being written
    process.stderr.write(`${details}\n`, () => {
        process.stdout.write(`${JSON.stringify({ error: message })}\n`, () => process.exit(1));
    });
}

async function autoScroll(page) {
    await page.evaluate(async (maxScrollMs) => {
        await new Promise((resolve) => {
            var distance = window.innerHeight || 1000; // One screen per step
            var startTime = Date.now();
            var timer = setInterval(() => {
                var before = window.scrollY;
                // Instant even if the page asks for smooth scrolling, which would fall behind
                window.scrollBy({ top: distance, behavior: "instant" });

                // Stop at the bottom, where the page doesn't scroll any further, or after maxScrollMs.
                if(window.scrollY <= before || Date.now() - startTime >= maxScrollMs){
                    clearInterval(timer);
                    resolve();
                }
            }, 100); // Interval between scrolls
        });
    }, MAX_SCROLL_MS);
}

// The scripts, fetches and images that the page is still loading
function trackRequests(page) {
    const types = ["script", "fetch", "xhr", "image"];
    const requests = new Set();
    page.on("request", (request) => {
        try {
            if (types.includes(request.resourceType()) && request.frame() === page.mainFrame()) {
                requests.add(request);
            }
        } catch {
            // Service worker requests have no frame
        }
    });
    page.on("requestfinished", (request) => requests.delete(request));
    page.on("requestfailed", (request) => requests.delete(request));
    return requests;
}

// Until the page goes a second without loading any of them
async function waitForRequests(requests, timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    let quietSince = Date.now();
    while (Date.now() - quietSince < 1000 && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 100));
        if (requests.size > 0) {
            quietSince = Date.now();
        }
    }
}

// A redirect that Chrome follows, which is a step on the way to the response
function isRedirect(response) {
    return [301, 302, 303, 307, 308].includes(response.status()) && response.headers().location !== undefined;
}

// Until the page's HTML has all arrived, which can take a while, unlike resources that might never finish
async function waitForHtml(navigations) {
    const deadline = Date.now() + NAVIGATION_TIMEOUT_MS;
    const { response } = navigations;
    while (response && isPage(response) && !navigations.done.has(response.request()) && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 100));
    }
}

// Navigations that don't load a new document: no content, or a download
function leavesNoDocument(response) {
    return [204, 205].includes(response.status())
        || /^\s*attachment/i.test(response.headers()["content-disposition"] || "");
}

function mimeType(response) {
    return (response.headers()["content-type"] || "text/html").split(";")[0].trim().toLowerCase();
}

// What Chrome renders as a page; anything else is saved as the site sent it
function isPage(response) {
    return ["text/html", "application/xhtml+xml"].includes(mimeType(response));
}

function tooLarge() {
    return new Error(`The file is larger than ${MAX_FILE_BYTES / 1024 / 1024} MB`);
}

// Resolves to the value after ms, without keeping the process alive
function after(ms, value) {
    return new Promise((resolve) => setTimeout(resolve, ms, value).unref());
}

// A response that isn't a page, as the site sent it
async function readResponse(page, response, downloadsPath) {
    const type = mimeType(response);
    // Chrome's viewers (PDF, video, audio) keep the response to themselves, and Chrome keeps no copy of a large one
    const viewed = type === "application/pdf" || type.startsWith("video/") || type.startsWith("audio/");
    return (viewed ? null : await response.body().catch(() => null)) ?? downloadAgain(page, response, downloadsPath);
}

// The file, or null if it doesn't finish loading in time
async function readFile(page, response, downloadsPath) {
    if (Number(response.headers()["content-length"]) > MAX_FILE_BYTES) {
        throw tooLarge();
    }
    const reading = readResponse(page, response, downloadsPath);
    // Left behind if it takes too long
    reading.catch(() => {});
    const data = await Promise.race([reading, after(FILE_TIMEOUT_MS, null)]);
    if (data && data.length > MAX_FILE_BYTES) {
        throw tooLarge();
    }
    return data;
}

// What the downloads in the directory have written so far
async function downloadedBytes(directory) {
    const names = await fs.promises.readdir(directory);
    const sizes = await Promise.all(names.map(
        (name) => fs.promises.stat(path.join(directory, name)).then((stats) => stats.size, () => 0)
    ));
    return sizes.reduce((total, size) => total + size, 0);
}

// Chrome writes a download as it arrives, and a site can send one without saying its size; null if it fails
async function readDownload(download, response, downloadsPath) {
    const finished = download.path().then((filePath) => ({ filePath }), () => ({ failed: true }));
    let size = Number(response.headers()["content-length"]) || 0;
    while (size <= MAX_FILE_BYTES) {
        const result = await Promise.race([finished, after(100, null)]);
        if (result?.failed) {
            return null;
        }
        if (result) {
            if ((await fs.promises.stat(result.filePath)).size > MAX_FILE_BYTES) {
                throw tooLarge();
            }
            return fs.promises.readFile(result.filePath);
        }
        size = await downloadedBytes(downloadsPath);
    }
    await download.cancel();
    throw tooLarge();
}

// Chrome downloads the file again, which the page's Content-Security-Policy doesn't forbid
async function downloadAgain(page, response, downloadsPath) {
    const isMainFrame = (frame) => frame === page.mainFrame();
    const started = Promise.race([
        page.waitForEvent("download", { timeout: 0 }),
        // A sandboxed page, or a redirect to another site, navigates instead
        page.waitForEvent("framenavigated", { predicate: isMainFrame, timeout: 0 }).then(() => null),
    ]);
    await page.evaluate((url) => {
        const link = document.createElement("a");
        link.href = url;
        link.download = "";
        link.click();
    }, response.url());
    const download = await started;
    const file = download && await readDownload(download, response, downloadsPath);
    if (!file) {
        throw new Error("The file couldn't be fetched again");
    }
    return file;
}

// A new Chrome profile with printing off, since a page that prints would wait for the print dialog forever
function createProfile() {
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), "webscreencap-profile-"));
    fs.mkdirSync(path.join(profile, "Default"));
    fs.writeFileSync(path.join(profile, "Default", "Preferences"), JSON.stringify({ printing: { enabled: false } }));
    return profile;
}

// The extension of the file's name, if it looks like one
function fileExtension(name) {
    const extension = path.extname(name).toLowerCase();
    return /^\.[a-z0-9]{1,10}$/.test(extension) ? extension : "";
}

// Counts the main frame's navigations and keeps the last response and error
function trackNavigations(page) {
    const navigations = {
        started: 0, committed: Promise.resolve(), response: null, previous: null, latest: null, failure: null,
        // The navigation requests whose response has all arrived, or that failed
        done: new WeakSet(),
    };
    const isMainFrameNavigation = (request) => {
        try {
            return request.isNavigationRequest() && request.frame() === page.mainFrame();
        } catch {
            return false;
        }
    };
    // Until the navigation commits, or turns out not to load a new document
    const settled = () => new Promise((resolve) => {
        const done = () => {
            clearTimeout(timer);
            page.off("framenavigated", onFrame);
            page.off("response", onResponse);
            page.off("download", done);
            resolve();
        };
        const onFrame = (frame) => frame === page.mainFrame() && done();
        const onResponse = (response) => isMainFrameNavigation(response.request()) && leavesNoDocument(response) && done();
        const timer = setTimeout(done, NAVIGATION_TIMEOUT_MS).unref();
        page.on("framenavigated", onFrame);
        page.on("response", onResponse);
        page.on("download", done);
    });
    page.on("request", (request) => {
        // Chrome's error pages retry the failed navigation by themselves; that's not the page navigating
        if (isMainFrameNavigation(request) && !request.redirectedFrom() && !page.url().startsWith("chrome-error://")) {
            navigations.started++;
            navigations.committed = settled();
            // An error of an earlier navigation is no longer the page's
            navigations.failure = null;
        }
    });
    page.on("response", (response) => {
        if (isMainFrameNavigation(response.request()) && !isRedirect(response)) {
            // Also the responses that load no document
            navigations.latest = response;
            if (!leavesNoDocument(response)) {
                navigations.previous = navigations.response;
                navigations.response = response;
            }
        }
    });
    // Chrome also downloads the types it can't show, and then the page stays as it was
    page.on("download", (download) => {
        const { response } = navigations;
        // The last response, unless it was already left out, as it is when the page reloads its URL into a download
        if (response && response === navigations.latest && response.url() === download.url().split("#")[0]) {
            navigations.response = navigations.previous;
        }
    });
    page.on("requestfinished", (request) => isMainFrameNavigation(request) && navigations.done.add(request));
    page.on("requestfailed", (request) => {
        if (!isMainFrameNavigation(request)) {
            return;
        }
        navigations.done.add(request);
        const { errorText } = request.failure();
        // Chrome turns HTTP errors without a body into failures, but they have a response
        if (errorText !== HTTP_ERROR_WITHOUT_BODY) {
            navigations.failure = `${errorText} at ${request.url()}`;
        }
    });
    return navigations;
}

// The page's HTML, with the content of open shadow roots as <template shadowrootmode> elements
function readHtml(blockerMarker) {
    // The ad blocker's styles hide ads in the screenshot, but the HTML is the page's; they're out only while it's read
    const styles = [...document.querySelectorAll("style")].filter((style) => style.textContent.startsWith(blockerMarker));
    const places = styles.map((style) => [style.parentNode, style.nextSibling]);
    styles.forEach((style) => style.remove());
    const roots = [];
    const collect = (root) => {
        for (const element of root.querySelectorAll("*")) {
            if (element.shadowRoot) {
                roots.push(element.shadowRoot);
                collect(element.shadowRoot);
            }
        }
    };
    collect(document);
    const html = document.documentElement;
    const doctype = document.doctype ? new XMLSerializer().serializeToString(document.doctype) : "";
    let content;
    if (!roots.length || !html || !(document instanceof HTMLDocument)) {
        content = doctype + (html ? html.outerHTML : "");
    } else {
        const tag = html.cloneNode(false).outerHTML.replace(/<\/html>$/, "");
        content = doctype + tag + html.getHTML({ serializableShadowRoots: true, shadowRoots: roots }) + "</html>";
    }
    // The last one first, so that the node each one goes before is back already
    for (let i = styles.length - 1; i >= 0; i--) {
        places[i][0].insertBefore(styles[i], places[i][1]);
    }
    return content;
}

// Cuts a page longer than maxHeight there; returns the styles to put back, or null
function capHeight(maxHeight) {
    const elements = [document.documentElement, document.body];
    if (Math.max(...elements.map((element) => element.scrollHeight)) <= maxHeight) {
        return null;
    }
    const styles = elements.map((element) => element.getAttribute("style"));
    for (const element of elements) {
        element.style.setProperty("height", `${maxHeight}px`, "important");
        element.style.setProperty("overflow", "hidden", "important");
    }
    return styles;
}

function restoreHeight(styles) {
    [document.documentElement, document.body].forEach((element, i) => {
        if (styles[i] === null) {
            element.removeAttribute("style");
        } else {
            // As written, with the declarations Chrome doesn't know too
            element.setAttribute("style", styles[i]);
        }
    });
}

// The HTML is saved as UTF-8, so it must declare UTF-8, whatever the page said
function declareUtf8() {
    if (!document.head) {
        return;
    }
    document.querySelectorAll("meta[charset], meta[http-equiv='content-type' i]").forEach((meta) => meta.remove());
    const meta = document.createElement("meta");
    meta.setAttribute("charset", "utf-8");
    document.head.prepend(meta);
}

// Starts over when the page navigates by itself; after MAX_NAVIGATIONS, captures without scrolling
async function capturePage(page, navigations) {
    const requests = trackRequests(page);
    for (let i = 0; ; i++) {
        const started = navigations.started;
        const navigated = () => navigations.started !== started;
        const hurry = i >= MAX_NAVIGATIONS;

        await navigations.committed;
        await waitForHtml(navigations);
        await page.waitForLoadState("load", { timeout: LOAD_TIMEOUT_MS }).catch(() => {});
        if (!hurry) {
            await page.waitForLoadState("networkidle", { timeout: NETWORK_SETTLE_MS }).catch(() => {});
            if (navigated()) {
                continue;
            }
        }

        try {
            // A file that Chrome shows (PDF, image, JSON...) has nothing to scroll, and no HTML of its own
            const isFile = navigations.response !== null && !isPage(navigations.response);
            if (!hurry && !isFile) {
                requests.clear();
                await autoScroll(page);
                await waitForRequests(requests, LAZY_CONTENT_TIMEOUT_MS);
            }
            // Without a body (SVG, for instance), fullPage would wait for one forever
            const hasBody = await page.evaluate(() => document.body !== null);
            // Chrome draws the whole page for a full-page screenshot, which can take minutes on a long one
            const styles = hasBody ? await page.evaluate(capHeight, MAX_SCREENSHOT_HEIGHT) : null;
            // With fullPage, the clip is relative to the whole page
            const screenshot = await page.screenshot(hasBody ? {
                fullPage: true,
                clip: { x: 0, y: 0, width: MAX_SCREENSHOT_WIDTH, height: MAX_SCREENSHOT_HEIGHT }
            } : {});
            if (styles) {
                await page.evaluate(restoreHeight, styles);
            }
            let htmlContent = null;
            if (!isFile) {
                await page.evaluate(declareUtf8);
                htmlContent = await page.evaluate(readHtml, STYLE_MARKER);
            }
            if (hurry || !navigated()) {
                return { screenshot, htmlContent };
            }
        } catch (error) {
            // Navigations to about:blank or to prerendered pages send no request first
            const interrupted = navigated() || String(error?.message).includes("Execution context was destroyed");
            if (!interrupted || i >= MAX_NAVIGATIONS + 2) {
                throw error;
            }
        }
    }
}

// The URL as Chrome handles it: a page, a file that Chrome shows, a download, or no content at all
async function captureUrl(page, url, navigations, downloadsPath, proxyErrorHeader) {
    const downloading = page.waitForEvent("download", { timeout: 0 }).catch(() => null);
    try {
        await page.goto(url, { waitUntil: "commit", timeout: NAVIGATION_TIMEOUT_MS });
    } catch (error) {
        const message = String(error?.message);
        const response = navigations.latest;
        if (response && message.includes("Download is starting")) {
            const download = await downloading;
            const file = await readDownload(download, response, downloadsPath);
            if (!file) {
                throw new Error("The download failed");
            }
            return { response, file, fileName: download.suggestedFilename(), finalUrl: response.url() };
        }
        // Chrome stays on its blank page
        if (response && [204, 205].includes(response.status())) {
            return { response, finalUrl: response.url() };
        }
        if (!navigations.response || !message.includes(HTTP_ERROR_WITHOUT_BODY)) {
            throw error;
        }
    }
    const { screenshot, htmlContent } = await capturePage(page, navigations);
    const { response } = navigations;
    // Chrome shows its own page for network errors, and for HTTP errors without a body (no HTML to save)
    if (page.url().startsWith("chrome-error://")) {
        if (navigations.failure || !response) {
            throw new Error(navigations.failure || "The page navigated to an address that couldn't be loaded");
        }
        return { response, screenshot, html: "", finalUrl: response.url() };
    }
    const proxyError = response && response.headers()[proxyErrorHeader];
    if (proxyError) {
        throw new Error(`${decodeURIComponent(proxyError)} at ${page.url()}`);
    }
    if (htmlContent === null) {
        const file = await readFile(page, response, downloadsPath);
        return { response, screenshot, file, fileName: new URL(response.url()).pathname, finalUrl: response.url() };
    }
    return { response, screenshot, html: htmlContent, finalUrl: page.url() };
}

(async () => {
    const args = process.argv.slice(2);
    if (args.length !== 1) {
        return fail("Expected exactly one URL", "Usage: node scraper.js <URL>");
    }

    const url = args[0];

    // unref() so it doesn't keep the process alive
    setTimeout(() => {
        fail("Timed out while processing the URL", `Timed out after ${SCRIPT_TIMEOUT_MS} ms.`);
    }, SCRIPT_TIMEOUT_MS).unref();

    try {
        // Bad URL settings fail here, before doing any work
        const bucketName = process.env.BUCKET_NAME;
        const fileUrl = fileUrlBuilder(bucketName);
        // And so does a Chrome that would let WebRTC get past the proxy
        await assertWebRtcPolicy();

        // Only public http(s) targets, as the proxy does for everything the page loads
        await assertPublicUrl(url);
        const proxy = await startEgressProxy();
        // Downloads are saved there, so that their size can be watched as they arrive
        const downloadsPath = fs.mkdtempSync(path.join(os.tmpdir(), "webscreencap-downloads-"));
        const profile = createProfile();

        // Everything goes through the proxy, loopback included; the flags close the bypasses
        const context = await chromium.launchPersistentContext(profile, {
            // Google Chrome with a real window, like a regular browser
            channel: "chrome",
            headless: false,
            chromiumSandbox: true,
            proxy: { server: proxy.url },
            downloadsPath,
            args: [
                "--disable-quic",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                "--window-size=1920,1080",
            ],
            // The window's own size and Chrome's own user agent, as in a regular browser
            viewport: null,
        }).finally(() => {
            // Registered after the launch, so that Patchright stops Chrome before they're removed
            process.on("exit", () => {
                fs.rmSync(downloadsPath, { recursive: true, force: true });
                fs.rmSync(profile, { recursive: true, force: true });
            });
        });
        // The tab Chrome opens with
        const page = context.pages()[0];
        // The capture is of the page: what it opens in other windows is closed right away
        page.on("popup", (popup) => popup.close().catch(() => {}));

        // Enable ad blocking (before navigating, so it sees every request)
        const blocker = await loadLatestBlocker();
        await blocker.enableBlockingInPage(page);

        const capture = await captureUrl(page, url, trackNavigations(page), downloadsPath, proxy.errorHeader);
        await context.close();
        proxy.close();

        // Random file names
        const stamp = `${Date.now()}-${crypto.randomBytes(32).toString("hex")}`;
        const headers = capture.response ? capture.response.headers() : {};
        const saved = { screenshotUrl: null, htmlUrl: null, fileUrl: null };
        if (capture.screenshot) {
            await uploadToTigris(bucketName, `screenshot-${stamp}.png`, capture.screenshot, "image/png");
            saved.screenshotUrl = await fileUrl(`screenshot-${stamp}.png`);
        }
        if (capture.html !== undefined) {
            await uploadToTigris(bucketName, `page-${stamp}.html`, capture.html, "text/html; charset=utf-8");
            saved.htmlUrl = await fileUrl(`page-${stamp}.html`);
        }
        if (capture.file) {
            const fileName = `file-${stamp}${fileExtension(capture.fileName)}`;
            // How the site sent it, so that it opens or downloads as it did there
            const disposition = /^[\x20-\x7e]+$/.test(headers["content-disposition"] || "") ? headers["content-disposition"] : undefined;
            await uploadToTigris(bucketName, fileName, capture.file, headers["content-type"] || "application/octet-stream", disposition);
            saved.fileUrl = await fileUrl(fileName);
        }

        console.log(JSON.stringify({
            ...saved,
            contentType: headers["content-type"] || null,
            httpStatus: capture.response ? capture.response.status() : null,
            finalUrl: capture.finalUrl
        }));
    } catch (error) {
        // Only the first line: Playwright appends its call log below it
        const message = String(error?.message ?? error).split("\n")[0];
        fail(message, util.format("Error processing screenshot:", error));
    }
})();
