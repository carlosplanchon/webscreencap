const { chromium } = require("patchright");
const { S3Client, GetObjectCommand, PutObjectCommand } = require("@aws-sdk/client-s3");
const { getSignedUrl } = require("@aws-sdk/s3-request-presigner");
const crypto = require("crypto");
const util = require("util");
const { STYLE_MARKER, loadBlocker, removeBlockerStyles } = require("./adblocker");
const { PROXY_ERROR_HEADER, assertPublicUrl, startEgressProxy } = require("./egress_proxy");

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

async function uploadToTigris(bucketName, fileName, fileContent, contentType) {
    const params = {
        Bucket: bucketName,
        Key: fileName,
        Body: fileContent,
        ContentType: contentType
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
            var totalHeight = 0;
            var distance = window.innerHeight || 1000; // One screen per step
            var startTime = Date.now();
            var timer = setInterval(() => {
                // SVG documents have no body
                var element = document.body || document.documentElement;
                var scrollHeight = element ? element.scrollHeight : 0;
                window.scrollBy(0, distance);
                totalHeight += distance;
  
                // Stop at the bottom, or after maxScrollMs.
                if(totalHeight >= scrollHeight - window.innerHeight || Date.now() - startTime >= maxScrollMs){
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

// Navigations that don't load a new document: no content, or a download
function leavesNoDocument(response) {
    return [204, 205].includes(response.status())
        || /^\s*attachment/i.test(response.headers()["content-disposition"] || "");
}

// Counts the main frame's navigations and keeps the last response and error
function trackNavigations(page) {
    const navigations = { started: 0, committed: Promise.resolve(), response: null, failure: null };
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
        }
    });
    page.on("response", (response) => {
        if (isMainFrameNavigation(response.request()) && !leavesNoDocument(response)) {
            navigations.response = response;
        }
    });
    page.on("requestfailed", (request) => {
        const { errorText } = request.failure();
        // Chrome turns HTTP errors without a body into failures, but they have a response
        if (isMainFrameNavigation(request) && errorText !== HTTP_ERROR_WITHOUT_BODY) {
            navigations.failure = `${errorText} at ${request.url()}`;
        }
    });
    return navigations;
}

// The page's HTML, with the content of open shadow roots as <template shadowrootmode> elements
function readHtml() {
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
    if (!roots.length || !html || !(document instanceof HTMLDocument)) {
        return doctype + (html ? html.outerHTML : "");
    }
    const tag = html.cloneNode(false).outerHTML.replace(/<\/html>$/, "");
    return doctype + tag + html.getHTML({ serializableShadowRoots: true, shadowRoots: roots }) + "</html>";
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
            element.style.cssText = styles[i];
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
        await page.waitForLoadState("load", { timeout: LOAD_TIMEOUT_MS }).catch(() => {});
        if (!hurry) {
            await page.waitForLoadState("networkidle", { timeout: NETWORK_SETTLE_MS }).catch(() => {});
            if (navigated()) {
                continue;
            }
        }

        try {
            if (!hurry) {
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
            // The ad blocker's styles hide ads in the screenshot, but the HTML is the page's
            await page.evaluate(removeBlockerStyles, STYLE_MARKER);
            await page.evaluate(declareUtf8);
            const htmlContent = await page.evaluate(readHtml);
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

        // Only public http(s) targets, as the proxy does for everything the page loads
        await assertPublicUrl(url);
        const proxy = await startEgressProxy();

        // Everything goes through the proxy, loopback included; the flags close the bypasses
        const browser = await chromium.launch({
            // Google Chrome with a real window, like a regular browser
            channel: "chrome",
            headless: false,
            chromiumSandbox: true,
            proxy: { server: proxy.url },
            args: [
                "--disable-quic",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                "--window-size=1920,1080",
            ]
        });
        // The window's own size and Chrome's own user agent, as in a regular browser
        const context = await browser.newContext({ viewport: null });
        const page = await context.newPage();

        // Enable ad blocking (before navigating, so it sees every request)
        const blocker = await loadBlocker();
        await blocker.enableBlockingInPage(page);

        // Navigate to the URL
        const navigations = trackNavigations(page);
        try {
            await page.goto(url, { waitUntil: "commit", timeout: NAVIGATION_TIMEOUT_MS });
        } catch (error) {
            if (!navigations.response || !String(error?.message).includes(HTTP_ERROR_WITHOUT_BODY)) {
                throw error;
            }
        }
        let { screenshot, htmlContent } = await capturePage(page, navigations);
        let finalUrl = page.url();
        // Chrome shows its own page for network errors, and for HTTP errors without a body (no HTML to save)
        if (finalUrl.startsWith("chrome-error://")) {
            if (navigations.failure || !navigations.response) {
                throw new Error(navigations.failure || "The page navigated to an address that couldn't be loaded");
            }
            htmlContent = "";
            finalUrl = navigations.response.url();
        }
        const proxyError = navigations.response && navigations.response.headers()[PROXY_ERROR_HEADER];
        if (proxyError) {
            throw new Error(`${decodeURIComponent(proxyError)} at ${finalUrl}`);
        }
        const httpStatus = navigations.response ? navigations.response.status() : null;

        await browser.close();
        proxy.close();

        // Generate random hex string for filenames
        const randomHex = crypto.randomBytes(32).toString("hex");
        const screenshotFileName = `screenshot-${Date.now()}-${randomHex}.png`;
        const htmlFileName = `page-${Date.now()}-${randomHex}.html`;

        await uploadToTigris(bucketName, screenshotFileName, screenshot, "image/png");
        await uploadToTigris(bucketName, htmlFileName, htmlContent, "text/html; charset=utf-8");

        console.log(JSON.stringify({
            screenshotUrl: await fileUrl(screenshotFileName),
            htmlUrl: await fileUrl(htmlFileName),
            httpStatus,
            finalUrl
        }));
    } catch (error) {
        // Only the first line: Playwright appends its call log below it
        const message = String(error?.message ?? error).split("\n")[0];
        fail(message, util.format("Error processing screenshot:", error));
    }
})();
