const { chromium } = require("playwright-extra");
const stealth = require("puppeteer-extra-plugin-stealth")();
const { S3Client, GetObjectCommand, PutObjectCommand } = require("@aws-sdk/client-s3");
const { getSignedUrl } = require("@aws-sdk/s3-request-presigner");
const crypto = require("crypto");
const util = require("util");
const { loadBlocker } = require("./adblocker");
const { assertPublicUrl, startEgressProxy } = require("./egress_proxy");

// For the page's first response, and for the pages it navigates to by itself.
const NAVIGATION_TIMEOUT_MS = 60000;
// How long to wait for the "load" event before capturing anyway (a resource may never finish).
const LOAD_TIMEOUT_MS = 10000;
// How many of the redirects the page makes by itself (JavaScript, meta refresh) are followed.
const MAX_NAVIGATIONS = 5;
// After the page's "load" event, how long to wait at most for the network to go quiet.
// Waiting for "networkidle" alone (discouraged by Playwright) never ends on pages with
// constant traffic (analytics, chats, polling), so those hit the 60 s timeout.
const NETWORK_SETTLE_MS = 5000;
// Upper bound for autoScroll, so pages with infinite scroll can't keep it going forever.
const MAX_SCROLL_MS = 60000;
// Hard deadline for the whole run (celery_task.py kills it at SCRAPER_TIMEOUT as a backstop).
const SCRIPT_TIMEOUT_MS = 150000;
// Screenshots are cut at this size. Full-page captures grow with the page: a long
// Wikipedia article alone ran the 1 GB worker out of memory, while capped at 8192 px
// it peaked at ~700 MB. Raise it only together with the VM's memory.
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

// How the file URLs in the result are built (FILE_URL_MODE):
//  - "presigned" (default): signed URLs, so the bucket can stay private. They expire
//    after PRESIGNED_URL_EXPIRES_IN seconds (default: 1 day, like the task result).
//  - "public": PUBLIC_FILE_BASE_URL followed by the file name, for public buckets
//    (on Tigris, https://<bucket>.t3.tigrisfiles.io).
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

// celery_task.py reads the result from stdout: one JSON line with the file URLs,
// or with an error message short enough to show to API clients. The details
// (stack traces, Playwright call logs) go to stderr and only reach the worker logs.
function fail(message, details) {
    // process.exit() can cut off output that's still being written, so wait for both.
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
                var scrollHeight = document.body.scrollHeight;
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

// Counts the navigations of the main frame (server redirects are part of each one)
// and keeps the error of the last one that failed.
function trackNavigations(page) {
    const navigations = { started: 0, committed: Promise.resolve(), failure: null };
    const isMainFrameNavigation = (request) => {
        try {
            return request.isNavigationRequest() && request.frame() === page.mainFrame();
        } catch {
            return false;
        }
    };
    page.on("request", (request) => {
        if (isMainFrameNavigation(request) && !request.redirectedFrom()) {
            navigations.started++;
            navigations.committed = page.waitForEvent("framenavigated", {
                predicate: (frame) => frame === page.mainFrame(),
                timeout: NAVIGATION_TIMEOUT_MS,
            }).catch(() => {});
        }
    });
    page.on("requestfailed", (request) => {
        if (isMainFrameNavigation(request)) {
            navigations.failure = `${request.failure().errorText} at ${request.url()}`;
        }
    });
    return navigations;
}

// Waits for the page and captures it. If the page navigates by itself in the meantime,
// it starts over on the new page.
async function capturePage(page, navigations) {
    for (let i = 0; ; i++) {
        const started = navigations.started;
        const navigated = () => navigations.started !== started && i < MAX_NAVIGATIONS;

        await navigations.committed;
        await page.waitForLoadState("load", { timeout: LOAD_TIMEOUT_MS }).catch(() => {});
        await page.waitForLoadState("networkidle", { timeout: NETWORK_SETTLE_MS }).catch(() => {});
        if (navigated()) {
            continue;
        }

        try {
            await autoScroll(page);
            // With fullPage, the clip is relative to the whole page (shorter pages are kept whole).
            const screenshot = await page.screenshot({
                fullPage: true,
                clip: { x: 0, y: 0, width: MAX_SCREENSHOT_WIDTH, height: MAX_SCREENSHOT_HEIGHT }
            });
            const htmlContent = await page.content();
            if (!navigated()) {
                return { screenshot, htmlContent };
            }
        } catch (error) {
            if (!navigated()) {
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

    // unref() so the timer doesn't keep the process alive after a successful run.
    setTimeout(() => {
        fail("Timed out while processing the URL", `Timed out after ${SCRIPT_TIMEOUT_MS} ms.`);
    }, SCRIPT_TIMEOUT_MS).unref();

    try {
        // Bad URL settings fail here, before doing any work
        const bucketName = process.env.BUCKET_NAME;
        const fileUrl = fileUrlBuilder(bucketName);

        // Only public http(s) targets; the proxy applies the same rule to
        // everything the page loads afterwards.
        await assertPublicUrl(url);
        const proxy = await startEgressProxy();

        chromium.use(stealth);
        // Playwright sends loopback through the proxy too (Chromium alone would skip it).
        const browser = await chromium.launch({
            headless: true,
            proxy: { server: proxy.url }
        });
        // No userAgent here: the stealth plugin sets a current desktop Chrome one,
        // matching the engine that renders the page.
        const context = await browser.newContext({
            viewport: { width: 1920, height: 1080 }
        });
        // No popups, as in a regular browser without a click. A capture has no use for
        // them, and with Playwright 1.64 and the adblocker, a popup plus an iframe to a
        // blocked address kept the page from ever finishing loading.
        await context.addInitScript(() => { window.open = () => null; });
        const page = await context.newPage();

        // Enable ad blocking (before navigating, so it sees every request)
        const blocker = await loadBlocker();
        await blocker.enableBlockingInPage(page);

        // Navigate to the URL
        const navigations = trackNavigations(page);
        await page.goto(url, { waitUntil: "domcontentloaded", timeout: NAVIGATION_TIMEOUT_MS });
        const { screenshot, htmlContent } = await capturePage(page, navigations);
        if (page.url().startsWith("chrome-error://")) {
            throw new Error(navigations.failure || "The page navigated to an address that couldn't be loaded");
        }

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
            htmlUrl: await fileUrl(htmlFileName)
        }));
    } catch (error) {
        // Only the first line of the message: Playwright appends its call log below it.
        const message = String(error?.message ?? error).split("\n")[0];
        fail(message, util.format("Error processing screenshot:", error));
    }
})();
