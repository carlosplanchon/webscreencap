// Downloads and updates of the ad-blocking lists, run with `node --test`
const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const path = require("path");
const { createRequire } = require("module");
const { loadBlocker, loadLatestBlocker, updateBlocker } = require("../src/adblocker.js");
// The packages are installed in src/
const { Request } = createRequire(require.resolve("../src/adblocker.js"))("@ghostery/adblocker-playwright");

// Only the filter lists fail; resources.json is fine
const rateLimited = async (url) => url.endsWith("resources.json")
    ? new Response("{}")
    : new Response("429: Too Many Requests", { status: 429 });
// Lists with a single rule, so that they can be told apart from the image's
const oneRule = async (url) => new Response(url.endsWith("resources.json") ? "{}" : "||only-in-the-update.example^");
// Starts answering, and then never sends the rest
const stalled = async (url, { signal }) => new Response(new ReadableStream({
    start(controller) {
        signal.addEventListener("abort", () => controller.error(signal.reason));
    },
}));

function blocks(blocker, url) {
    return blocker.match(Request.fromRawDetails({ url, sourceUrl: "https://site.example/", type: "script" })).match;
}

// As if the lists had been updated a day ago
function ageByADay(file) {
    const dayAgo = new Date(Date.now() - 24 * 60 * 60 * 1000);
    fs.utimesSync(file, dayAgo, dayAgo);
}

test("a failed list download fails instead of building an empty blocker", async () => {
    const cachePath = "/tmp/adblocker-test-engine.bin";
    await assert.rejects(loadBlocker(rateLimited, cachePath), /HTTP 429/);
    assert.ok(!fs.existsSync(cachePath));
});

test("captures load the updated lists, and nothing partial is left behind", async () => {
    // In a directory that doesn't exist yet
    const updatedPath = path.join(fs.mkdtempSync("/tmp/adblocker-test-"), "cache", "engine.bin");
    await updateBlocker(oneRule, updatedPath);
    assert.ok(blocks(await loadLatestBlocker(updatedPath), "https://only-in-the-update.example/ad.js"));
    assert.deepStrictEqual(fs.readdirSync(path.dirname(updatedPath)), ["engine.bin"]);
});

test("lists updated less than a day ago aren't downloaded again", async () => {
    const updatedPath = "/tmp/adblocker-test-recent.bin";
    fs.rmSync(updatedPath, { force: true });
    let downloads = 0;
    const counted = async (url) => {
        downloads++;
        return oneRule(url);
    };
    assert.strictEqual(await updateBlocker(counted, updatedPath), true);
    const lists = downloads;
    assert.strictEqual(await updateBlocker(counted, updatedPath), false);
    assert.strictEqual(downloads, lists);
    ageByADay(updatedPath);
    assert.strictEqual(await updateBlocker(counted, updatedPath), true);
    assert.strictEqual(downloads, 2 * lists);
});

test("a failed update keeps the lists there were", async () => {
    const updatedPath = "/tmp/adblocker-test-kept.bin";
    fs.rmSync(updatedPath, { force: true });
    await updateBlocker(oneRule, updatedPath);
    ageByADay(updatedPath);
    await assert.rejects(updateBlocker(rateLimited, updatedPath), /HTTP 429/);
    assert.ok(blocks(await loadLatestBlocker(updatedPath), "https://only-in-the-update.example/ad.js"));
});

test("a list that stops arriving fails the update in time", async () => {
    const start = Date.now();
    await assert.rejects(updateBlocker(stalled, "/tmp/adblocker-test-stalled.bin", 500), /timeout/);
    assert.ok(Date.now() - start < 5000);
    assert.ok(!fs.existsSync("/tmp/adblocker-test-stalled.bin"));
});

test("before any update, captures load the image's lists", async () => {
    const blocker = await loadLatestBlocker("/tmp/adblocker-test-missing.bin");
    assert.ok(!blocks(blocker, "https://only-in-the-update.example/ad.js"));
    assert.ok(blocks(blocker, "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js"));
});
