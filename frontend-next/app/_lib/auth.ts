"use client";

import { getApiBase } from "./api-base";

export const API_BASE = getApiBase();
export const AUTH_EVENT = "agrisky-auth-changed";
const LEGACY_TOKEN_KEY = "agrisky.portal.token";

export type SessionMe = {
  username: string;
  holder_name: string;
  role: string;
};

function clearLegacyTokenStorage() {
  if (typeof window !== "undefined") localStorage.removeItem(LEGACY_TOKEN_KEY);
}

export function notifyAuthChanged() {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(AUTH_EVENT));
}

export async function loginWithPassword(username: string, password: string) {
  clearLegacyTokenStorage();
  const response = await fetch(`${API_BASE}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ username, password })
  });
  const payload = (await response.json().catch(() => ({}))) as { detail?: string };
  if (!response.ok) {
    throw new Error(payload.detail || "登录失败");
  }
}

export async function fetchMe() {
  clearLegacyTokenStorage();
  const response = await fetch(`${API_BASE}/api/v1/me`, {
    credentials: "include"
  });
  if (response.status === 401) return null;
  const payload = (await response.json().catch(() => ({}))) as SessionMe & { detail?: string };
  if (!response.ok) {
    throw new Error(payload.detail || "加载账号信息失败");
  }
  return payload;
}

export async function logoutSession() {
  clearLegacyTokenStorage();
  try {
    await fetch(`${API_BASE}/api/v1/auth/logout`, {
      method: "POST",
      credentials: "include"
    });
  } finally {
    notifyAuthChanged();
  }
}
