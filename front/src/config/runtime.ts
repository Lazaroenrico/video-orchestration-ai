const stripTrailingSlash = (value: string) => value.replace(/\/+$/, "");

function envString(name: "VITE_API_BASE_URL" | "VITE_MEDIA_BASE_URL"): string {
  const value = import.meta.env[name];
  return typeof value === "string" ? stripTrailingSlash(value.trim()) : "";
}

export const runtimeConfig = {
  apiBaseUrl: envString("VITE_API_BASE_URL"),
  mediaBaseUrl: envString("VITE_MEDIA_BASE_URL"),
};
