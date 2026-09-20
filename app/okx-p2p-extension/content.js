const PRICE_MIN = 1000;
const PRICE_MAX = 200000;
let timer = null;
let scanning = false;

function visible(element) {
  const style = getComputedStyle(element);
  const rect = element.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
}

function digits(value) {
  const cleaned = String(value || "").replace(/\D/g, "");
  return cleaned ? Number(cleaned) : 0;
}

function currencyValues(text) {
  const values = [];
  const regex = /(\d[\d.,\s]{1,18})\s*(?:VND|₫)/gi;
  for (const match of text.matchAll(regex)) {
    const value = digits(match[1]);
    if (value > 0) values.push({value, index: match.index || 0});
  }
  return values;
}

function parseLimitRange(text, price) {
  const explicit = text.match(
    /(?:limit|limits|hạn mức|giới hạn|amount)[^\d]{0,40}(\d[\d.,\s]{1,18})\s*(?:VND|₫)?\s*(?:-|–|~|đến|to)\s*(\d[\d.,\s]{1,18})\s*(?:VND|₫)?/i
  );
  if (explicit) {
    return [digits(explicit[1]), digits(explicit[2])];
  }
  const amounts = currencyValues(text)
    .map(item => item.value)
    .filter(value => value !== price && value >= Math.max(50000, price * 2));
  return [amounts[0] || 0, amounts[1] || 0];
}

function usefulName(value) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (text.length < 2 || text.length > 100 || /\d{3,}/.test(text)) return "";
  if (/^(buy|sell|mua|bán|limit|limits|hạn mức|available|khả dụng|payment|thanh toán|orders?|lệnh|VND|USDT)$/i.test(text)) return "";
  return text;
}

function detectNickname(root, text) {
  const selectors = [
    "[data-testid*='nick']",
    "[data-testid*='name']",
    "[class*='nickname']",
    "[class*='merchant-name']",
    "[class*='user-name']",
    "[class*='advertiser-name']"
  ];
  for (const selector of selectors) {
    const element = root.querySelector(selector);
    const name = usefulName(element?.textContent);
    if (name) return name;
  }
  const lines = text.split(/\n+/).map(usefulName).filter(Boolean);
  return lines[0] || "Merchant chưa xác định";
}

function stat(text, patterns) {
  for (const pattern of patterns) {
    const match = text.match(pattern);
    if (match) return digits(match[1]);
  }
  return 0;
}

function parseOffer(root) {
  const text = String(root.innerText || "").replace(/\u00a0/g, " ").trim();
  if (text.length < 20 || text.length > 2500) return null;
  const priceToken = currencyValues(text).find(item => item.value >= PRICE_MIN && item.value <= PRICE_MAX);
  if (!priceToken) return null;
  const price = priceToken.value;
  let [minAmount, maxAmount] = parseLimitRange(text, price);
  if (minAmount && maxAmount && minAmount > maxAmount) [minAmount, maxAmount] = [maxAmount, minAmount];
  return {
    nickname: detectNickname(root, text),
    price,
    min_amount: minAmount,
    max_amount: maxAmount,
    account_days: stat(text, [/(\d+)\s*(?:days?|ngày)/i]),
    completed_orders: stat(text, [/(\d+)\s*(?:completed orders?|lệnh hoàn tất|đơn hoàn tất)/i]),
    total_orders: stat(text, [/(\d+)\s*(?:total orders?|tổng số lệnh|tổng đơn)/i])
  };
}

function candidateRoots() {
  const selectors = [
    "tbody tr",
    "[role='row']",
    "[data-testid*='advertiser']",
    "[data-testid*='offer']",
    "[class*='advertiser-card']",
    "[class*='advertiserCard']",
    "[class*='merchant-card']",
    "[class*='merchantCard']"
  ];
  const roots = new Set();
  for (const selector of selectors) {
    document.querySelectorAll(selector).forEach(element => {
      if (visible(element)) roots.add(element);
    });
  }

  document.querySelectorAll("span,p,strong").forEach(element => {
    if (!visible(element) || !/(?:VND|₫)/i.test(element.textContent || "")) return;
    let current = element;
    for (let level = 0; level < 7 && current; level += 1, current = current.parentElement) {
      const text = current.innerText || "";
      if (text.length >= 40 && text.length <= 2000 && currencyValues(text).length >= 2) {
        roots.add(current);
        break;
      }
    }
  });
  return [...roots];
}

function extractOffers() {
  const deduped = new Map();
  for (const root of candidateRoots()) {
    const offer = parseOffer(root);
    if (!offer) continue;
    const key = `${offer.nickname.toLocaleLowerCase()}|${offer.price}|${offer.min_amount}|${offer.max_amount}`;
    const existing = deduped.get(key);
    if (!existing || (root.innerText || "").length < existing.textLength) {
      deduped.set(key, {...offer, textLength: (root.innerText || "").length});
    }
  }
  return [...deduped.values()]
    .sort((a, b) => a.price - b.price)
    .slice(0, 100)
    .map(({textLength: _ignored, ...offer}) => offer);
}

function detectSide(configured) {
  if (configured === "BUY" || configured === "SELL") return configured;
  const value = `${location.pathname} ${location.search}`.toLowerCase();
  if (/(?:^|[\/_?=&-])buy(?:$|[\/_?=&-])/.test(value)) return "BUY";
  if (/(?:^|[\/_?=&-])sell(?:$|[\/_?=&-])/.test(value)) return "SELL";
  return "";
}

async function scanAndSend() {
  if (scanning) return {ok: false, message: "Đang quét"};
  scanning = true;
  try {
    const config = await chrome.storage.local.get({enabled: false, side: "AUTO"});
    const side = detectSide(config.side);
    if (!side) throw new Error("Không xác định được BUY/SELL; hãy chọn chiều trong Extension");
    const offers = extractOffers();
    if (!offers.length) throw new Error("Chưa nhận diện được quảng cáo nào trên trang hiện tại");
    const response = await chrome.runtime.sendMessage({
      type: "PUSH_OFFERS",
      payload: {
        side,
        offers,
        page_url: location.href,
        captured_at: new Date().toISOString()
      }
    });
    if (!response?.ok) throw new Error(response?.message || "Không gửi được dữ liệu");
    return {ok: true, message: `Đã đọc ${offers.length} quảng cáo ${side}`, offers};
  } finally {
    scanning = false;
  }
}

async function schedule() {
  if (timer) clearInterval(timer);
  const config = await chrome.storage.local.get({enabled: false, intervalSeconds: 10});
  if (!config.enabled) return;
  const seconds = Math.min(300, Math.max(5, Number(config.intervalSeconds) || 10));
  timer = setInterval(() => scanAndSend().catch(() => {}), seconds * 1000);
  setTimeout(() => scanAndSend().catch(() => {}), 1200);
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "SCAN_NOW") {
    scanAndSend().then(sendResponse).catch(error => sendResponse({ok: false, message: error.message}));
    return true;
  }
  if (message?.type === "CONFIG_CHANGED") {
    schedule().then(() => sendResponse({ok: true}));
    return true;
  }
  return false;
});

chrome.storage.onChanged.addListener(schedule);
schedule();
