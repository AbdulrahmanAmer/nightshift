// Depth layer 0 (docs/design/installer-ui.md section 3.2). The licence-free generated canvas (public/canvas.svg, the same
// marbled swirl the dashboard uses) under the theme wash, shown in the 16 px margin around the frame. If the image does
// not load, PageBackdrop (hairline curves and dots) takes over.
import { useEffect, useState } from "react";
import { PageBackdrop } from "@ref12";

export function SetupBackdrop() {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    const img = new Image();
    img.decoding = "async";
    img.src = "canvas.svg";
    img.decode().then(() => live && setSrc(img.src)).catch(() => live && setFailed(true));
    return () => { live = false; };
  }, []);
  if (failed) return <PageBackdrop />;
  return <div aria-hidden className="ds-canvas">{src && <img src={src} alt="" className="ds-canvas-img" />}</div>;
}
