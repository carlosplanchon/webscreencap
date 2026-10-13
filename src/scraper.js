const { chromium } = require("playwright-extra");
const stealth = require("puppeteer-extra-plugin-stealth")();
const { S3Client, PutObjectCommand } = require("@aws-sdk/client-s3");
const crypto = require("crypto");
const util = require("util");
const { loadBlocker } = require("./adblocker");
const { assertPublicUrl, startEgressProxy } = require("./egress_proxy");

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
// it peaked at ~600 MB. Raise it only together with the VM's memory.
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
        const data = await s3.send(command);
        // console.log(`File uploaded successfully: ${fileName}`);
        // AWS_ENDPOINT_URL_S3 already includes the scheme (e.g. https://fly.storage.tigris.dev).
        return `${process.env.AWS_ENDPOINT_URL_S3.replace(/\/+$/, "")}/${bucketName}/${fileName}`;
    } catch (error) {
        console.error("Error uploading to Tigris:", error);
        throw new Error("Failed to upload file to Tigris Object Storage.");
    }
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
    await page.evaluate(async (maxScrollMs) => {
        await new Promise((resolve) => {
            var totalHeight = 0;
            var distance = 1000; // Scroll distance per step
            var startTime = Date.now();
            var timer = setInterval(() => {
                var scrollHeight = document.body.scrollHeight;
                window.scrollBy(0, distance);
                totalHeight += distance;
  
                // Stop at the bottom, or after maxScrollMs if the page keeps growing.
                if(totalHeight >= scrollHeight || Date.now() - startTime >= maxScrollMs){
                    clearInterval(timer);
                    resolve();
                }
            }, 2000); // Interval between scrolls
        });
    }, MAX_SCROLL_MS);
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
        const page = await context.newPage();

        // Enable ad blocking
        const blocker = await loadBlocker();
        blocker.enableBlockingInPage(page);

        // Navigate to the URL
        await page.goto(url, { waitUntil: "load", timeout: 60000 });
        await page.waitForLoadState("networkidle", { timeout: NETWORK_SETTLE_MS }).catch(() => {});
        await autoScroll(page);

        // Capture screenshot and HTML content
        // With fullPage, the clip is relative to the whole page (shorter pages are kept whole).
        const screenshot = await page.screenshot({
            fullPage: true,
            clip: { x: 0, y: 0, width: MAX_SCREENSHOT_WIDTH, height: MAX_SCREENSHOT_HEIGHT }
        });
        const htmlContent = await page.content();

        await browser.close();
        proxy.close();

        // Generate random hex string for filenames
        const randomHex = crypto.randomBytes(32).toString("hex");
        const bucketName = process.env.BUCKET_NAME;
        const screenshotFileName = `screenshot-${Date.now()}-${randomHex}.png`;
        const htmlFileName = `page-${Date.now()}-${randomHex}.html`;

        const screenshotUrl = await uploadToTigris(bucketName, screenshotFileName, screenshot, "image/png");
        const htmlUrl = await uploadToTigris(bucketName, htmlFileName, htmlContent, "text/html");

        console.log(JSON.stringify({ screenshotUrl, htmlUrl }));
    } catch (error) {
        // Only the first line of the message: Playwright appends its call log below it.
        const message = String(error?.message ?? error).split("\n")[0];
        fail(message, util.format("Error processing screenshot:", error));
    }
})();
