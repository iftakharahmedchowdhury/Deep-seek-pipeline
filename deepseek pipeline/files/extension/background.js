// Background worker for the DeepSeek Local Bridge.
//  1. "bridge": relays calls from the page's content script to the local bridge server.
//     Doing the fetch here (not in the page) avoids CORS / private-network issues.
//  2. "gate": ONE shared send gate for every DeepSeek tab/instance. It guarantees
//     - only one sender at a time (a lease that expires, so a crashed tab can't block forever)
//     - a minimum gap between any two messages, which grows when DeepSeek says "too frequent"
//       and shrinks slowly while sending succeeds.
//     State lives in chrome.storage.local, so it survives the worker being put to sleep.

let gateQueue = Promise.resolve();
const serial = (fn) => {
  const p = gateQueue.then(fn);
  gateQueue = p.then(() => {}, () => {});
  return p;
};

const LEASE_MS = 45000;

async function gateOp(msg) {
  const { gate } = await chrome.storage.local.get("gate");
  const g = { lastSendAt: 0, owner: null, until: 0, gapMs: 0, ...(gate || {}) };
  const now = Date.now();
  const base = msg.baseGap || 10000;
  const max = msg.maxGap || 60000;
  const gap = Math.min(max, Math.max(base, g.gapMs || 0));
  const save = () => chrome.storage.local.set({ gate: g });
  let out = { ok: true, gap };

  switch (msg.op) {
    case "acquire": {
      if (g.owner && g.owner !== msg.id && g.until > now) {
        out = { ok: false, wait: Math.min(g.until - now, 3000), gap };
        break;
      }
      const since = now - g.lastSendAt;
      if (since < gap) {
        out = { ok: false, wait: gap - since, gap };
        break;
      }
      g.owner = msg.id;
      g.until = now + LEASE_MS;
      await save();
      break;
    }
    case "release":
      if (g.owner === msg.id) { g.owner = null; g.until = 0; }
      if (msg.sentAt) g.lastSendAt = Math.max(g.lastSendAt, msg.sentAt);
      await save();
      break;
    case "touch": // a manual send by the user
      g.lastSendAt = now;
      await save();
      break;
    case "limited": // DeepSeek rejected a message: slow down
      g.gapMs = Math.min(max, Math.max(gap, base) * 1.5);
      await save();
      out = { ok: true, gap: g.gapMs };
      break;
    case "ok": // accepted: relax slowly back toward the base gap
      g.gapMs = Math.max(base, gap * 0.92);
      await save();
      out = { ok: true, gap: g.gapMs };
      break;
    default:
      out = { ok: false, error: "unknown gate op" };
  }
  return out;
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "gate") {
    serial(() => gateOp(msg)).then(sendResponse, (e) => sendResponse({ ok: false, error: String(e) }));
    return true; // async response
  }

  if (msg.type !== "bridge") return;

  chrome.storage.local.get(["token", "port"]).then(async ({ token, port }) => {
    try {
      const r = await fetch(`http://127.0.0.1:${port || 8765}${msg.path}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Bridge-Token": token || "",
        },
        body: JSON.stringify(msg.body || {}),
      });
      sendResponse({ ok: r.ok, data: await r.json() });
    } catch (e) {
      sendResponse({ ok: false, data: { error: String(e) } });
    }
  });

  return true; // keep the channel open for the async response
});
