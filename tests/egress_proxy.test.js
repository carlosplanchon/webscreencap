// Address policy of the egress proxy, run with `node --test`
const test = require("node:test");
const assert = require("node:assert");
const { isPublicAddress, assertPublicUrl } = require("../src/egress_proxy.js");

test("public addresses", () => {
    for (const address of ["8.8.8.8", "93.184.215.14", "11.250.0.20", "2606:4700:4700::1111", "::ffff:8.8.8.8"]) {
        assert.ok(isPublicAddress(address), address);
    }
});

test("loopback, private, link-local and other special addresses", () => {
    for (const address of [
        "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1",
        "0.0.0.0", "224.0.0.1", "255.255.255.255", "::", "::1", "fe80::1",
        "fdaa:0:18:a7b:7d:f066:b83b:2", "::ffff:127.0.0.1", "64:ff9b::7f00:1",
    ]) {
        assert.ok(!isPublicAddress(address), address);
    }
});

test("URLs rejected before loading", async () => {
    for (const [url, message] of [
        ["file:///etc/passwd", /Only http and https/],
        ["javascript:alert(1)", /Only http and https/],
        ["http://127.0.0.1/", /127\.0\.0\.1 is not a public address/],
        ["http://2130706433/", /127\.0\.0\.1 is not a public address/],
        ["http://0x7f.1/", /127\.0\.0\.1 is not a public address/],
        ["http://[::1]/", /::1 is not a public address/],
        ["http://[::ffff:127.0.0.1]/", /is not a public address/],
        ["http://localhost/", /localhost resolves to .*, which is not a public address/],
    ]) {
        await assert.rejects(assertPublicUrl(url), message, url);
    }
});

test("public URLs pass", async () => {
    await assert.doesNotReject(assertPublicUrl("http://93.184.215.14/"));
    await assert.doesNotReject(assertPublicUrl("https://[2606:4700:4700::1111]/"));
});
