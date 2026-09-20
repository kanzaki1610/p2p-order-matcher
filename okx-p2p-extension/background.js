const DEFAULT_SERVER = "https://p2p-order-matcher.onrender.com";

function validServerUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname.endsWith(".onrender.com");
  } catch {
    return false;
  }
}

async function saveStatus(status) {
  await chrome.storage.local.set({
    lastStatus: {...status, at: new Date().toISOString()}
  });
}

async function pushOffers(payload) {
  const config = await chrome.storage.local.get({
    serverUrl: DEFAULT_SERVER,
    bridgeSecret: ""
  });
  const serverUrl = String(config.serverUrl || "").replace(/\/+$/, "");
  if (!validServerUrl(serverUrl)) {
    throw new Error("URL Render không hợp lệ");
  }
  if (!config.bridgeSecret) {
    throw new Error("Chưa nhập OKX_BROWSER_BRIDGE_SECRET");
  }

  const response = await fetch(`${serverUrl}/dashboard/api/bridge/offers`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Bridge-Key": config.bridgeSecret
    },
    body: JSON.stringify(payload),
    credentials: "omit",
    cache: "no-store"
  });
  let body = {};
  try {
    body = await response.json();
  } catch {
    // The status code below still produces a useful diagnostic.
  }
  if (!response.ok) {
    throw new Error(body.detail || `Render trả về HTTP ${response.status}`);
  }
  return body;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== "PUSH_OFFERS") return false;
  pushOffers(message.payload)
    .then(async result => {
      const status = {
        ok: true,
        message: `Đã gửi ${message.payload.offers.length} quảng cáo ${message.payload.side}`,
        offerCount: message.payload.offers.length,
        side: message.payload.side
      };
      await saveStatus(status);
      sendResponse({ok: true, result});
    })
    .catch(async error => {
      const status = {ok: false, message: error.message};
      await saveStatus(status);
      sendResponse(status);
    });
  return true;
});

chrome.runtime.onInstalled.addListener(async () => {
  const current = await chrome.storage.local.get(["serverUrl", "side", "intervalSeconds", "enabled"]);
  await chrome.storage.local.set({
    serverUrl: current.serverUrl || DEFAULT_SERVER,
    side: current.side || "AUTO",
    intervalSeconds: current.intervalSeconds || 10,
    enabled: current.enabled ?? false
  });
});
