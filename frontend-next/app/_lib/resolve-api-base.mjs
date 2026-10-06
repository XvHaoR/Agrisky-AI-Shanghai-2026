const KNOWN_PORT_MAP = Object.freeze(
  /** @type {const} */ ({ "3000": "8000", "3001": "8001", "3011": "8011" })
);

/**
 * @param {string} hostname
 * @returns {boolean}
 */
export function isLocalHost(hostname) {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "[::1]" || hostname === "::1";
}

/**
 * Compute an origin-only API base URL with no trailing slash.
 *
 * @param {{
 *   envApiBase?: string | null | undefined;
 *   hostname?: string | undefined;
 *   port?: string | undefined;
 *   protocol?: string | undefined;
 * }} params
 * @returns {string}
 */
export function computeApiBase({ envApiBase, hostname, port, protocol } = {}) {
  if (envApiBase) {
    return envApiBase.replace(/\/+$/, "");
  }

  if (hostname && protocol) {
    if (isLocalHost(hostname)) {
      if (port && Object.hasOwn(KNOWN_PORT_MAP, port)) {
        return `${protocol}//${hostname}:${KNOWN_PORT_MAP[port]}`;
      }

      if (port && /^3\d{3}$/.test(port)) {
        return `${protocol}//${hostname}:8${port.slice(1)}`;
      }
    }

    if (!port) {
      return `${protocol}//${hostname}`;
    }

    return `${protocol}//${hostname}:${port}`;
  }

  return "http://localhost:8000";
}
