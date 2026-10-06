"use client";

import { computeApiBase } from "./resolve-api-base.mjs";

/**
 * Resolve the API base URL at runtime.
 *
 * Priority:
 * 1. Explicit NEXT_PUBLIC_API_BASE_URL (trim trailing slashes).
 * 2. Browser-side local inference when the env var is absent.
 * 3. Same-origin fallback for production-style browser origins.
 * 4. Build-time/server-side fallback.
 */
export function resolveApiBase(): string {
  const envApiBase = process.env.NEXT_PUBLIC_API_BASE_URL;

  if (typeof window !== "undefined") {
    return computeApiBase({
      envApiBase,
      hostname: window.location.hostname,
      port: window.location.port,
      protocol: window.location.protocol
    });
  }

  return computeApiBase({ envApiBase });
}

let cachedApiBase: string | undefined;

export function getApiBase(): string {
  if (cachedApiBase === undefined) {
    cachedApiBase = resolveApiBase();
  }
  return cachedApiBase;
}

export function assetUrl(path?: string | null): string | null {
  if (!path) return null;
  if (/^https?:\/\//i.test(path)) return path;
  return `${getApiBase()}${path}`;
}
