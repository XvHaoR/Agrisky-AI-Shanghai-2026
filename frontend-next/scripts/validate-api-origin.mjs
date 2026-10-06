const DEFAULT_ALLOWED_LOCAL = new Set(["localhost", "127.0.0.1", "::1"]);

export function validatePublicApiOrigin(raw, options = {}) {
  const requireHttps = options.requireHttps ?? process.env.AGRISKY_REQUIRE_HTTPS_API_ORIGIN === "true";
  const value = String(raw || "").trim();
  if (!value) {
    throw new Error("NEXT_PUBLIC_API_BASE_URL is required");
  }

  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error("NEXT_PUBLIC_API_BASE_URL must be an absolute URL");
  }

  if (!["http:", "https:"].includes(url.protocol)) {
    throw new Error("NEXT_PUBLIC_API_BASE_URL must use http or https");
  }
  if (url.username || url.password) {
    throw new Error("NEXT_PUBLIC_API_BASE_URL must not include credentials");
  }
  if (url.pathname !== "/" || url.search || url.hash) {
    throw new Error("NEXT_PUBLIC_API_BASE_URL must be an origin only, without path, query, or hash");
  }
  if (value.endsWith("/")) {
    throw new Error("NEXT_PUBLIC_API_BASE_URL must not include a trailing slash");
  }

  const isLocal = DEFAULT_ALLOWED_LOCAL.has(url.hostname);
  if (requireHttps && url.protocol !== "https:" && !isLocal) {
    throw new Error("Production NEXT_PUBLIC_API_BASE_URL must use https");
  }

  return true;
}

if (import.meta.url === `file://${process.argv[1].replace(/\\/g, "/")}`) {
  validatePublicApiOrigin(process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000");
}
