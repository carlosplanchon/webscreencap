// Downloads of the ad-blocking lists, run with `node --test`
const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const { loadBlocker } = require("../src/adblocker.js");

test("a failed list download fails instead of building an empty blocker", async () => {
    const cachePath = "/tmp/adblocker-test-engine.bin";
    // Only the filter lists fail; resources.json is fine
    const rateLimited = async (url) => url.endsWith("resources.json")
        ? new Response("{}")
        : new Response("429: Too Many Requests", { status: 429 });
    await assert.rejects(loadBlocker(rateLimited, cachePath), /HTTP 429/);
    assert.ok(!fs.existsSync(cachePath));
});
