import "./investment-panel-runtime.js?v=0.4.0-r37";

// V15 score-help guard.
// The historical market-score bubble must not open merely because result DOM
// appeared beneath a stationary mouse pointer. The existing runtime handles
// ordinary help targets; this bridge suppresses score-label hover events and
// re-opens score help only after a genuine mouse movement. Touch remains on the
// runtime's normal click/tap path.
const SCORE_HELP = ".signal-help";
let lastMouseX = null;
let lastMouseY = null;
let hoveredScore = null;

const pathTarget = (event) => {
  const path = typeof event?.composedPath === "function" ? event.composedPath() : [];
  return path.find((node) => node?.classList?.contains?.("signal-help")) ||
    event?.target?.closest?.(SCORE_HELP) || null;
};

const dispatchHelpKey = (target, key) => {
  if (!target) return;
  target.dispatchEvent(new KeyboardEvent("keydown", {
    key,
    code: key === "Enter" ? "Enter" : "Escape",
    bubbles: true,
    composed: true,
    cancelable: true,
  }));
};

const removeScoreNativeTitles = (root = document) => {
  root.querySelectorAll?.(`${SCORE_HELP}[title]`).forEach((node) => node.removeAttribute("title"));
};

const observer = new MutationObserver((mutations) => {
  for (const mutation of mutations) {
    if (mutation.type === "attributes" && mutation.target?.matches?.(SCORE_HELP)) {
      mutation.target.removeAttribute("title");
    }
    for (const node of mutation.addedNodes || []) {
      if (node.nodeType === Node.ELEMENT_NODE) removeScoreNativeTitles(node);
    }
  }
});

const start = () => {
  removeScoreNativeTitles();
  observer.observe(document.documentElement || document, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ["title"],
  });
};

const moveIsReal = (event) => {
  const dx = Number(event?.movementX);
  const dy = Number(event?.movementY);
  if (Number.isFinite(dx) && Number.isFinite(dy) && (dx !== 0 || dy !== 0)) {
    lastMouseX = Number(event.clientX);
    lastMouseY = Number(event.clientY);
    return true;
  }
  const x = Number(event?.clientX);
  const y = Number(event?.clientY);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return false;
  const real = lastMouseX !== null && (x !== lastMouseX || y !== lastMouseY);
  lastMouseX = x;
  lastMouseY = y;
  return real;
};

document.addEventListener("pointermove", (event) => {
  if (event?.pointerType !== "mouse") return;
  const realMove = moveIsReal(event);
  const target = pathTarget(event);
  if (target) {
    event.stopPropagation();
    if (realMove && hoveredScore !== target) {
      hoveredScore = target;
      dispatchHelpKey(target, "Enter");
    }
  } else if (hoveredScore && realMove) {
    dispatchHelpKey(hoveredScore, "Escape");
    hoveredScore = null;
  }
}, true);

document.addEventListener("pointerenter", (event) => {
  if (event?.pointerType !== "mouse" || !pathTarget(event)) return;
  event.stopPropagation();
}, true);

document.addEventListener("pointerleave", (event) => {
  if (event?.pointerType !== "mouse") return;
  const target = pathTarget(event);
  if (!target) return;
  event.stopPropagation();
}, true);

start();
