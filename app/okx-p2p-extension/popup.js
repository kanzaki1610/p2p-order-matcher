const DEFAULT_SERVER = "https://p2p-order-matcher.onrender.com";
const ids = ["serverUrl", "bridgeSecret", "side", "intervalSeconds", "enabled"];
const element = id => document.getElementById(id);

function showStatus(message, ok = null) {
  const status = element("status");
  status.textContent = message;
  status.className = `status ${ok === true ? "ok" : ok === false ? "bad" : ""}`;
}

async function restore() {
  const config = await chrome.storage.local.get({
    serverUrl: DEFAULT_SERVER,
    bridgeSecret: "",
    side: "AUTO",
    intervalSeconds: 10,
    enabled: false,
    lastStatus: null
  });
  for (const id of ids) {
    const input = element(id);
    if (input.type === "checkbox") input.checked = Boolean(config[id]);
    else input.value = config[id] ?? "";
  }
  if (config.lastStatus) {
    const time = new Date(config.lastStatus.at).toLocaleString("vi-VN");
    showStatus(`${config.lastStatus.message}\n${time}`, config.lastStatus.ok);
  }
}

async function activeOkxTab() {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  if (!tab?.id || !/^https:\/\/([a-z0-9-]+\.)?okx\.com\//i.test(tab.url || "")) {
    throw new Error("Hãy mở trang OKX P2P trong tab hiện tại");
  }
  return tab;
}

element("save").addEventListener("click", async () => {
  const config = {
    serverUrl: element("serverUrl").value.trim().replace(/\/+$/, ""),
    bridgeSecret: element("bridgeSecret").value.trim(),
    side: element("side").value,
    intervalSeconds: Math.min(300, Math.max(5, Number(element("intervalSeconds").value) || 10)),
    enabled: element("enabled").checked
  };
  await chrome.storage.local.set(config);
  try {
    const tab = await activeOkxTab();
    await chrome.tabs.sendMessage(tab.id, {type: "CONFIG_CHANGED"});
    showStatus("Đã lưu cấu hình.", true);
  } catch (error) {
    showStatus(`Đã lưu. ${error.message}`, null);
  }
});

element("scan").addEventListener("click", async () => {
  showStatus("Đang đọc trang OKX…", null);
  try {
    const tab = await activeOkxTab();
    const response = await chrome.tabs.sendMessage(tab.id, {type: "SCAN_NOW"});
    showStatus(response?.message || "Đã quét xong", Boolean(response?.ok));
  } catch (error) {
    showStatus(error.message, false);
  }
});

chrome.storage.onChanged.addListener(changes => {
  if (!changes.lastStatus?.newValue) return;
  const value = changes.lastStatus.newValue;
  showStatus(`${value.message}\n${new Date(value.at).toLocaleString("vi-VN")}`, value.ok);
});

restore();
