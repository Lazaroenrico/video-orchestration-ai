import type { Creator } from "./contracts";

function renderableVoiceUri(uri: string | null | undefined): string | null {
  const value = uri?.trim();
  if (!value) return null;
  if (
    value.startsWith("/") ||
    value.startsWith("./") ||
    value.startsWith("../") ||
    /^https?:\/\//i.test(value) ||
    /^data:audio\//i.test(value) ||
    /^blob:/i.test(value)
  ) {
    return value;
  }
  return null;
}

export function creatorVoiceUri(creator: Creator): string | null {
  return (
    renderableVoiceUri(creator.voice_preview_uri) ??
    renderableVoiceUri(creator.voice) ??
    renderableVoiceUri(creator.voice_ref)
  );
}
