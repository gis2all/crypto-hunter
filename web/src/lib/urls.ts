/** Return an absolute HTTP(S) URL that is safe to put in an anchor. */
export function safeHttpUrl(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const text = raw.trim();
  if (!text || /[\u0000-\u0020\u007f\\]/.test(text)) return null;
  try {
    const url = new URL(text);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    if (url.username || url.password) return null;
    if (url.port && url.port !== "80" && url.port !== "443") return null;
    const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, "");
    if (host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local")) {
      return null;
    }
    if (!host.includes(".") && !host.includes(":")) return null;
    if (isPrivateIpv4(host)) return null;
    if (host.includes(":")) {
      if (host === "::" || host === "::1" || /^(fe80|fc|fd)/i.test(host)) return null;
      const mapped = host.match(/^::ffff:(.+)$/i);
      if (mapped) {
        const ipv4 = mappedIpv4(mapped[1]);
        if (ipv4 && isPrivateIpv4(ipv4)) return null;
      }
    }
    return url.href;
  } catch {
    return null;
  }
}

function mappedIpv4(tail: string): string | null {
  if (tail.includes(".")) return tail;
  const parts = tail.split(":");
  if (parts.length !== 2) return null;
  const high = Number.parseInt(parts[0], 16);
  const low = Number.parseInt(parts[1], 16);
  if (!Number.isFinite(high) || !Number.isFinite(low)) return null;
  return `${high >> 8}.${high & 255}.${low >> 8}.${low & 255}`;
}

function isPrivateIpv4(host: string): boolean {
  const match = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (!match) return false;
  const [a, b, c] = match.slice(1).map(Number);
  return (
    a === 0 || a === 10 || a === 127 ||
    (a === 100 && b >= 64 && b <= 127) ||
    (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && (b === 0 || b === 168)) ||
    (a === 198 && (b === 18 || b === 19)) ||
    (a === 198 && b === 51 && c === 100) ||
    (a === 203 && b === 0 && c === 113) ||
    a >= 224
  );
}
