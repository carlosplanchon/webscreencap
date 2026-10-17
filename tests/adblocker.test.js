// Downloads and updates of the ad-blocking lists, run with `node --test`
const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
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

function blocks(blocker, url) {
    return blocker.match(Request.fromRawDetails({ url, sourceUrl: "https://site.example/", type: "script" })).match;
}

test("a failed list download fails instead of building an empty blocker", async () => {
    const cachePath = "/tmp/adblocker-test-engine.bin";
    await assert.rejects(loadBlocker(rateLimited, cachePath), /HTTP 429/);
    assert.ok(!fs.existsSync(cachePath));
});

test("captures load the updated lists, and nothing partial is left behind", async () => {
    const updatedPath = "/tmp/adblocker-test-updated.bin";
    fs.rmSync(updatedPath, { force: true });
    await updateBlocker(oneRule, updatedPath);
    assert.ok(blocks(await loadLatestBlocker(updatedPath), "https://only-in-the-update.example/ad.js"));
    assert.deepStrictEqual(fs.readdirSync("/tmp").filter((name) => name.startsWith("adblocker-test-updated.bin.")), []);
});

test("a failed update keeps the lists there were", async () => {
    const updatedPath = "/tmp/adblocker-test-kept.bin";
    await updateBlocker(oneRule, updatedPath);
    await assert.rejects(updateBlocker(rateLimited, updatedPath), /HTTP 429/);
    assert.ok(blocks(await loadLatestBlocker(updatedPath), "https://only-in-the-update.example/ad.js"));
});

test("before any update, captures load the image's lists", async () => {
    const blocker = await loadLatestBlocker("/tmp/adblocker-test-missing.bin");
    assert.ok(!blocks(blocker, "https://only-in-the-update.example/ad.js"));
    assert.ok(blocks(blocker, "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js"));
});
