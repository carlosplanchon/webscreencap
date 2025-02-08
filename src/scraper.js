const { chromium } = require("playwright-extra");
const { PlaywrightBlocker } = require("@ghostery/adblocker-playwright");
const { fetch } = require("cross-fetch");
const stealth = require("puppeteer-extra-plugin-stealth")();
const { S3Client, PutObjectCommand } = require("@aws-sdk/client-s3");
const crypto = require("crypto");
const randomUseragent = require("random-useragent");

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
        return `https://${process.env.AWS_ENDPOINT_URL_S3}/${bucketName}/${fileName}`;
    } catch (error) {
        console.error("Error uploading to Tigris:", error);
        throw new Error("Failed to upload file to Tigris Object Storage.");
    }
}

async function autoScroll(page) {
    await page.evaluate(async () => {
        await new Promise((resolve) => {
            var totalHeight = 0;
            var distance = 1000; // Scroll distance per step
            var timer = setInterval(() => {
                var scrollHeight = document.body.scrollHeight;
                window.scrollBy(0, distance);
                totalHeight += distance;
  
                if(totalHeight >= scrollHeight){
                    clearInterval(timer);
                    resolve();
                }
            }, 2000); // Interval between scrolls
        });
    });
}

(async () => {
    const args = process.argv.slice(2);
    if (args.length !== 1) {
        console.error("Usage: node script.js <URL>");
        process.exit(1);
    }

    const url = args[0];

    try {
        chromium.use(stealth);
        const browser = await chromium.launch({ headless: true });
        const context = await browser.newContext({ 
            viewport: { width: 1920, height: 1080 },
            userAgent: randomUseragent.getRandom() 
        });
        const page = await context.newPage();

        // Enable ad blocking
        const blocker = await PlaywrightBlocker.fromPrebuiltAdsAndTracking(fetch);
        blocker.enableBlockingInPage(page);

        // Navigate to the URL
        await page.goto(url, { waitUntil: "networkidle", timeout: 60000 });
        await autoScroll(page);

        // Capture screenshot and HTML content
        const screenshot = await page.screenshot({ fullPage: true });
        const htmlContent = await page.content();

        await browser.close();

        // Generate random hex string for filenames
        const randomHex = crypto.randomBytes(32).toString("hex");
        const bucketName = process.env.BUCKET_NAME;
        const screenshotFileName = `screenshot-${Date.now()}-${randomHex}.png`;
        const htmlFileName = `page-${Date.now()}-${randomHex}.html`;

        const screenshotUrl = await uploadToTigris(bucketName, screenshotFileName, screenshot, "image/png");
        const htmlUrl = await uploadToTigris(bucketName, htmlFileName, htmlContent, "text/html");

        console.log({
            message: "Screenshot and HTML saved successfully.",
            screenshotUrl,
            htmlUrl
        });
    } catch (error) {
        console.error("Error processing screenshot:", error);
        process.exit(1);
    }
})();
