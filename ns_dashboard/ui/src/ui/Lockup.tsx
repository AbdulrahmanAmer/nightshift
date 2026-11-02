/* Full logo lockup (mark + "NightShift" wordmark). The owner's logo-full.svg has a near-white "Night", which reads on the
   dark theme only; logo-full-light.svg is the same file with "Night" in the wordmark ink (#15131D) for light and soft.
   Both are plain images from public/brand, chosen by the active theme. */
import { useApp } from "../lib/app";

export function Lockup({ width = 200, className }: { width?: number; className?: string }) {
  const { theme } = useApp();
  const src = theme === "dark" ? "brand/logo-full.svg" : "brand/logo-full-light.svg";
  // The SVG's own box is 1145 x 825 (mark plus wordmark with margin); keep that ratio so nothing shifts while it loads.
  return <img src={src} width={width} height={Math.round((width * 825) / 1145)} alt="Nightshift" decoding="async" draggable={false} className={className} />;
}
