import { runtimeConfig } from "../config/runtime";

const ABSOLUTE_URL = /^[a-z][a-z\d+\-.]*:/i;

function joinUrl(baseUrl: string, path: string): string {
  if (!baseUrl || ABSOLUTE_URL.test(path)) return path;
  return new URL(path, `${baseUrl}/`).toString();
}

export function apiUrl(path: string): string {
  return joinUrl(runtimeConfig.apiBaseUrl, path);
}

export function sseUrl(path: string): string {
  return apiUrl(path);
}

export function mediaUrl(uri: string): string {
  const baseUrl = runtimeConfig.mediaBaseUrl || runtimeConfig.apiBaseUrl;
  return joinUrl(baseUrl, uri);
}
