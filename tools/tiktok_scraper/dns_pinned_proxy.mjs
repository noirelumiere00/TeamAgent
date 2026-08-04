import dns from "node:dns/promises";
import http from "node:http";
import net from "node:net";
import ipaddr from "ipaddr.js";

function normalizedIp(address) {
  let parsed = ipaddr.parse(String(address).toLowerCase().split("%", 1)[0]);
  if (parsed.kind() === "ipv6" && parsed.isIPv4MappedAddress()) {
    parsed = parsed.toIPv4Address();
  }
  return parsed;
}

export function isPublicIp(address) {
  try {
    return normalizedIp(address).range() === "unicast";
  } catch {
    return false;
  }
}

function parseBlockedCidrs(raw) {
  if (!raw) return [];
  return raw.split(",").filter(Boolean).map((value) => {
    const trimmed = value.trim();
    try {
      return ipaddr.parseCIDR(trimmed);
    } catch {
      throw new Error("MEDIA_BLOCKED_VPC_CIDRS contains an invalid CIDR");
    }
  });
}

function inBlockedCidr(address, blockedCidrs) {
  const parsed = normalizedIp(address);
  return blockedCidrs.some(([network, prefix]) => (
    network.kind() === parsed.kind() && parsed.match(network, prefix)
  ));
}

export async function resolvePinnedTarget(
  hostname,
  {
    lookup = dns.lookup,
    blockedCidrs = parseBlockedCidrs(process.env.MEDIA_BLOCKED_VPC_CIDRS || ""),
  } = {},
) {
  const host = String(hostname).replace(/^\[|\]$/g, "").replace(/\.$/, "").toLowerCase();
  if (!host || host.length > 253 || /[^a-z0-9.:-]/.test(host)) {
    throw new Error("proxy target hostname is invalid");
  }
  const answers = ipaddr.isValid(host)
    ? [{ address: host, family: normalizedIp(host).kind() === "ipv4" ? 4 : 6 }]
    : await lookup(host, { all: true, verbatim: true });
  if (!Array.isArray(answers) || answers.length === 0) {
    throw new Error("proxy target DNS returned no addresses");
  }
  const normalized = answers.map((answer) => ({
    address: normalizedIp(answer.address).toString(),
    family: normalizedIp(answer.address).kind() === "ipv4" ? 4 : 6,
  }));
  if (
    normalized.some(
      (answer) => !isPublicIp(answer.address) || inBlockedCidr(answer.address, blockedCidrs),
    )
  ) {
    throw new Error("private, reserved, link-local, or VPC address blocked");
  }
  normalized.sort((left, right) => (
    left.family - right.family || left.address.localeCompare(right.address)
  ));
  return normalized[0];
}

function parseConnectAuthority(authority) {
  let parsed;
  try {
    parsed = new URL(`https://${authority}`);
  } catch {
    throw new Error("proxy CONNECT authority is invalid");
  }
  if (
    parsed.username ||
    parsed.password ||
    parsed.pathname !== "/" ||
    parsed.search ||
    parsed.hash ||
    !String(authority).endsWith(":443")
  ) {
    throw new Error("proxy permits HTTPS CONNECT port 443 only");
  }
  return parsed.hostname;
}

export async function startDnsPinnedProxy({
  lookup = dns.lookup,
  connect = net.createConnection,
  blockedCidrs = parseBlockedCidrs(process.env.MEDIA_BLOCKED_VPC_CIDRS || ""),
} = {}) {
  const sockets = new Set();
  const server = http.createServer((_request, response) => {
    response.writeHead(403, { Connection: "close" });
    response.end();
  });
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.once("close", () => sockets.delete(socket));
  });
  server.on("connect", async (request, client, head) => {
    let upstream;
    // chromium 側が転送中にトンネルを切ると pipe の書き込みが client 側で
    // EPIPE を emit する。ハンドラ未設置だと 'error' が未処理例外となり
    // node ごと落ちる (Fargate 実測: 検索ページ遷移直後に write EPIPE で
    // プロセス全体がクラッシュし JSON 出力ゼロ)。両方向とも相手側を畳む。
    client.on("error", () => {
      if (upstream) upstream.destroy();
    });
    try {
      const hostname = parseConnectAuthority(request.url || "");
      const pinned = await resolvePinnedTarget(hostname, { lookup, blockedCidrs });
      upstream = connect({
        host: pinned.address,
        port: 443,
        family: pinned.family,
        servername: hostname,
      });
      sockets.add(upstream);
      upstream.once("close", () => sockets.delete(upstream));
      upstream.setTimeout(30_000, () => upstream.destroy(new Error("proxy peer timeout")));
      upstream.once("connect", () => {
        try {
          const peer = normalizedIp(upstream.remoteAddress || "").toString();
          if (peer !== pinned.address) {
            throw new Error("proxy connected peer differs from pinned DNS address");
          }
          client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
          if (head.length) upstream.write(head);
          client.pipe(upstream);
          upstream.pipe(client);
        } catch {
          upstream.destroy();
          client.destroy();
        }
      });
      upstream.on("error", () => client.destroy());
    } catch {
      if (upstream) upstream.destroy();
      client.destroy();
    }
  });
  server.on("clientError", (_error, socket) => socket.destroy());
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", reject);
      resolve();
    });
  });
  const address = server.address();
  if (!address || typeof address === "string") {
    server.close();
    throw new Error("DNS-pinned proxy failed to bind");
  }
  return {
    url: `http://127.0.0.1:${address.port}`,
    close: async () => {
      for (const socket of sockets) socket.destroy();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}
