/* Truncated text always carries its full text as a tooltip. Any element using Tailwind's `truncate` (or a line clamp) that
   actually overflows gets a title from its own text when it has none; the title is removed again if it stops overflowing.
   One observer for the whole app, so components do not each have to remember it. */
export function startAutoTitle(root: HTMLElement = document.body): () => void {
  let queued = false;
  const scan = () => {
    queued = false;
    root.querySelectorAll<HTMLElement>(".truncate, [class*='line-clamp']").forEach((el) => {
      const over = el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1;
      if (over && !el.title && !el.closest("[title]") && el.textContent?.trim()) { el.title = el.textContent.trim(); el.dataset.autoTitle = "1"; }
      else if (!over && el.dataset.autoTitle) { el.removeAttribute("title"); delete el.dataset.autoTitle; }
    });
  };
  const queue = () => { if (!queued) { queued = true; requestAnimationFrame(scan); } };
  const mo = new MutationObserver(queue);
  mo.observe(root, { childList: true, subtree: true, characterData: true });
  addEventListener("resize", queue); queue();
  return () => { mo.disconnect(); removeEventListener("resize", queue); };
}
