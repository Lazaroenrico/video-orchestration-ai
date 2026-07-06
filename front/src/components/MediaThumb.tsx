import type { Artifact } from "../types";
import { Icon } from "./Icon";

// Renders a renderable artifact (image/video/audio) or a neutral reference chip.
export function MediaThumb({
  artifact,
  className = "",
}: {
  artifact?: Artifact | null;
  className?: string;
}) {
  const base = `rounded-lg overflow-hidden bg-surface-container border border-surface-border ${className}`;
  if (!artifact || !artifact.renderable) {
    return (
      <div className={`${base} flex items-center justify-center text-on-surface-variant aspect-video`}>
        <Icon name="hourglass_top" />
      </div>
    );
  }
  if (artifact.media_type === "image") {
    return <img src={artifact.uri} alt="" className={`${base} object-cover w-full aspect-video`} />;
  }
  if (artifact.media_type === "video") {
    return <video src={artifact.uri} controls className={`${base} w-full aspect-video bg-black`} />;
  }
  if (artifact.media_type === "audio") {
    return (
      <div className={`${base} p-3 flex items-center gap-2`}>
        <Icon name="graphic_eq" className="text-ai-processing" />
        <audio src={artifact.uri} controls className="w-full" />
      </div>
    );
  }
  return (
    <div className={`${base} flex items-center justify-center text-on-surface-variant aspect-video`}>
      <Icon name="link" />
    </div>
  );
}
