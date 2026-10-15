const { chromium } = require("playwright-extra");
const stealth = require("puppeteer-extra-plugin-stealth")();
const { S3Client, GetObjectCommand, PutObjectCommand } = require("@aws-sdk/client-s3");
const { getSignedUrl } = require("@aws-sdk/s3-request-presigner");
const crypto = require("crypto");
const util = require("util");
const { loadBlocker } = require("./adblocker");
const { PROXY_ERROR_HEADER, assertPublicUrl, startEgressProxy } = require("./egress_proxy");

const NAVIGATION_TIMEOUT_MS = 60000;
// After this, the page is captured even if some resource never finished loading
const LOAD_TIMEOUT_MS = 10000;
// Redirects the page makes by itself (JavaScript, meta refresh) that are followed
const MAX_NAVIGATIONS = 5;
// Longest wait for the network to go quiet after "load"
const NETWORK_SETTLE_MS = 5000;
const MAX_SCROLL_MS = 60000;
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
    await page.evaluate(async ({ maxScrollMs, maxHeight }) => {
        await new Promise((resolve) => {
            var totalHeight = 0;
            var distance = 1000; // Scroll distance per step
            var startTime = Date.now();
            var timer = setInterval(() => {
                // SVG documents have no body
                var element = document.body || document.documentElement;
                var scrollHeight = element ? element.scrollHeight : 0;
                window.scrollBy(0, distance);
                totalHeight += distance;
  
                // Stop at the bottom or at the screenshot's height, or after maxScrollMs.
                if(totalHeight >= Math.min(scrollHeight, maxHeight) || Date.now() - startTime >= maxScrollMs){
                    clearInterval(timer);
                    resolve();
                }
            }, 2000); // Interval between scrolls
        });
    }, { maxScrollMs: MAX_SCROLL_MS, maxHeight: MAX_SCREENSHOT_HEIGHT });
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
        if (isMainFrameNavigation(request) && !request.redirectedFrom()) {
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
        if (isMainFrameNavigation(request)) {
            navigations.failure = `${request.failure().errorText} at ${request.url()}`;
        }
    });
    return navigations;
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
                await autoScroll(page);
            }
            // Without a body (SVG, for instance), fullPage would wait for one forever
            const hasBody = await page.evaluate(() => document.body !== null);
            // With fullPage, the clip is relative to the whole page
            const screenshot = await page.screenshot(hasBody ? {
                fullPage: true,
                clip: { x: 0, y: 0, width: MAX_SCREENSHOT_WIDTH, height: MAX_SCREENSHOT_HEIGHT }
            } : {});
            await page.evaluate(declareUtf8);
            const htmlContent = await page.content();
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

        chromium.use(stealth);
        // Everything goes through the proxy, loopback included; the flags close the bypasses
        const browser = await chromium.launch({
            headless: true,
            chromiumSandbox: true,
            proxy: { server: proxy.url },
            args: [
                "--disable-quic",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
            ]
        });
        // No userAgent: the stealth plugin sets one that matches the browser
        const context = await browser.newContext({
            viewport: { width: 1920, height: 1080 }
        });
        // No popups, as in a regular browser without a click
        await context.addInitScript(() => { window.open = () => null; });
        const page = await context.newPage();

        // Enable ad blocking (before navigating, so it sees every request)
        const blocker = await loadBlocker();
        await blocker.enableBlockingInPage(page);

        // Navigate to the URL
        const navigations = trackNavigations(page);
        await page.goto(url, { waitUntil: "commit", timeout: NAVIGATION_TIMEOUT_MS });
        const { screenshot, htmlContent } = await capturePage(page, navigations);
        if (page.url().startsWith("chrome-error://")) {
            throw new Error(navigations.failure || "The page navigated to an address that couldn't be loaded");
        }
        const proxyError = navigations.response && navigations.response.headers()[PROXY_ERROR_HEADER];
        if (proxyError) {
            throw new Error(`${decodeURIComponent(proxyError)} at ${page.url()}`);
        }
        const httpStatus = navigations.response ? navigations.response.status() : null;
        const finalUrl = page.url();

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
