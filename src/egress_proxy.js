// Proxy for Chromium that only lets pages reach public addresses

const dns = require("dns");
const http = require("http");
const net = require("net");

// Special-purpose address ranges (IANA registries). Anything else is public.
const NON_PUBLIC = new net.BlockList();
for (const [network, prefix] of [
    ["0.0.0.0", 8],        // "this network"
    ["10.0.0.0", 8],       // private
    ["100.64.0.0", 10],    // carrier-grade NAT
    ["127.0.0.0", 8],      // loopback
    ["169.254.0.0", 16],   // link-local, cloud metadata endpoints
    ["172.16.0.0", 12],    // private
    ["192.0.0.0", 24],     // IETF protocol assignments
    ["192.0.2.0", 24],     // documentation
    ["192.88.99.0", 24],   // 6to4 relay anycast
    ["192.168.0.0", 16],   // private
    ["198.18.0.0", 15],    // benchmarking
    ["198.51.100.0", 24],  // documentation
    ["203.0.113.0", 24],   // documentation
    ["224.0.0.0", 4],      // multicast
    ["240.0.0.0", 4],      // reserved, broadcast
]) {
    NON_PUBLIC.addSubnet(network, prefix, "ipv4");
}
for (const [network, prefix] of [
    ["::", 96],            // unspecified, loopback, IPv4-compatible
    ["64:ff9b::", 96],     // NAT64
    ["64:ff9b:1::", 48],   // local-use NAT64
    ["100::", 64],         // discard-only
    ["2001::", 23],        // IETF protocol assignments (Teredo, ...)
    ["2001:db8::", 32],    // documentation
    ["2002::", 16],        // 6to4
    ["fc00::", 7],         // unique local, including Fly's private network (fdaa::)
    ["fe80::", 10],        // link-local
    ["fec0::", 10],        // site-local (deprecated)
    ["ff00::", 8],         // multicast
]) {
    NON_PUBLIC.addSubnet(network, prefix, "ipv6");
}
// IPv4-mapped IPv6 addresses (::ffff:10.0.0.1) are matched against the IPv4 rules.

function isPublicAddress(address) {
    return !NON_PUBLIC.check(address, net.isIPv6(address) ? "ipv6" : "ipv4");
}

function nonPublicError(host, address) {
    const error = new Error(
        host === address
            ? `${host} is not a public address`
            : `${host} resolves to ${address}, which is not a public address`
    );
    error.code = "ENONPUBLIC";
    return error;
}

// Sockets skip the lookup for IP literals, so those are checked here
function checkedHost(hostname) {
    const host = hostname.replace(/^\[(.*)\]$/, "$1");
    if (net.isIP(host) && !isPublicAddress(host)) {
        throw nonPublicError(host, host);
    }
    return host;
}

// dns.lookup that fails unless every address of the host is public
function publicLookup(hostname, options, callback) {
    dns.lookup(hostname, { ...options, all: true }, (error, addresses) => {
        if (error) {
            return callback(error);
        }
        const blocked = addresses.find(({ address }) => !isPublicAddress(address));
        if (blocked) {
            return callback(nonPublicError(hostname, blocked.address));
        }
        if (options.all) {
            return callback(null, addresses);
        }
        callback(null, addresses[0].address, addresses[0].family);
    });
}

// Rejects URLs before Chromium tries them, with a clearer error
async function assertPublicUrl(url) {
    const { protocol, hostname } = new URL(url);
    if (protocol !== "http:" && protocol !== "https:") {
        throw new Error(`Only http and https URLs are allowed, got ${protocol}`);
    }
    const host = checkedHost(hostname);
    if (!net.isIP(host)) {
        await new Promise((resolve, reject) => {
            publicLookup(host, {}, (error) => (error ? reject(error) : resolve()));
        });
    }
}

// Hop-by-hop headers only apply to one connection, so they aren't forwarded.
const HOP_BY_HOP_HEADERS = [
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade",
];

function endToEndHeaders(headers) {
    const dropped = new Set(HOP_BY_HOP_HEADERS);
    for (const name of (headers.connection || "").split(",")) {
        dropped.add(name.trim().toLowerCase());
    }
    return Object.fromEntries(
        Object.entries(headers).filter(([name]) => !dropped.has(name))
    );
}

function errorStatus(error) {
    return error.code === "ENONPUBLIC" ? 403 : 502;
}

// Marks the proxy's own answers, so they aren't taken for the sites'
const PROXY_ERROR_HEADER = "x-egress-proxy-error";

function answerBlocked(res, status, error) {
    res.writeHead(status, {
        "content-type": "text/plain",
        [PROXY_ERROR_HEADER]: encodeURIComponent(error.message),
    }).end(`Blocked: ${error.message}\n`);
}

// close() also tears down open tunnels, so they can't keep the process alive
function startEgressProxy() {
    const sockets = new Set();
    const track = (socket) => {
        sockets.add(socket);
        socket.once("close", () => sockets.delete(socket));
        return socket;
    };

    // http:// requests arrive with the full URL: GET http://host/path
    const server = http.createServer((req, res) => {
        let target, host;
        try {
            target = new URL(req.url);
            if (target.protocol !== "http:") {
                throw new Error(`Unsupported protocol ${target.protocol}`);
            }
            host = checkedHost(target.hostname);
        } catch (error) {
            return answerBlocked(res, 403, error);
        }
        const upstream = http.request({
            host,
            port: target.port || 80,
            method: req.method,
            path: target.pathname + target.search,
            headers: endToEndHeaders(req.headers),
            lookup: publicLookup,
            agent: false,
        });
        upstream.on("socket", track);
        upstream.on("response", (upstreamRes) => {
            res.writeHead(upstreamRes.statusCode, endToEndHeaders(upstreamRes.headers));
            upstreamRes.pipe(res);
        });
        upstream.on("error", (error) => {
            if (res.headersSent) {
                return res.destroy();
            }
            answerBlocked(res, errorStatus(error), error);
        });
        res.on("close", () => upstream.destroy());
        req.pipe(upstream);
    });
    server.on("connection", track);

    // https:// and WebSockets are tunneled: CONNECT host:port
    server.on("connect", (req, clientSocket, head) => {
        let host, port;
        try {
            const target = new URL(`http://${req.url}`);
            host = checkedHost(target.hostname);
            port = Number(target.port) || 80;
        } catch {
            return clientSocket.end("HTTP/1.1 403 Forbidden\r\n\r\n");
        }
        let established = false;
        const upstream = track(net.connect({ host, port, lookup: publicLookup }));
        upstream.once("connect", () => {
            established = true;
            clientSocket.write("HTTP/1.1 200 Connection Established\r\n\r\n");
            upstream.write(head);
            upstream.pipe(clientSocket);
            clientSocket.pipe(upstream);
        });
        upstream.on("error", (error) => {
            if (established) {
                return clientSocket.destroy();
            }
            const status = errorStatus(error);
            clientSocket.end(`HTTP/1.1 ${status} ${http.STATUS_CODES[status]}\r\n\r\n`);
        });
        clientSocket.on("error", () => upstream.destroy());
        clientSocket.on("close", () => upstream.destroy());
    });

    return new Promise((resolve, reject) => {
        server.once("error", reject);
        server.listen(0, "127.0.0.1", () => {
            resolve({
                url: `http://127.0.0.1:${server.address().port}`,
                close() {
                    server.close();
                    for (const socket of sockets) {
                        socket.destroy();
                    }
                },
            });
        });
    });
}

module.exports = { PROXY_ERROR_HEADER, assertPublicUrl, isPublicAddress, startEgressProxy };
