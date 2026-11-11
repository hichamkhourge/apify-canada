#!/usr/bin/env node
/**
 * Puppeteer worker for the IPTVV WooCommerce free-trial checkout.
 *
 * Driven by iptvvcanada_automation.py (via puppeteer_client.py) over a JSON-lines
 * protocol: one command per line on stdin, one reply per line on stdout. All
 * logging goes to stderr so stdout stays a clean reply channel.
 *
 *   request : {"id": 1, "cmd": "launch", "args": {...}}
 *   reply   : {"id": 1, "ok": true,  "result": ...}
 *             {"id": 1, "ok": false, "error": {"type", "message", "diag"}}
 *
 * Cloudflare evasion follows https://www.browserless.io/blog/bypass-cloudflare-with-puppeteer:
 * puppeteer-extra + stealth plugin, a real headed Chrome (Xvfb in Docker), a
 * residential proxy (supplied by Python's local forwarding proxy), a randomized
 * viewport, and waiting out challenge pages / Turnstile iframes.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const readline = require('readline');
const { addExtra } = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');

const puppeteer = addExtra(require('puppeteer-core'));
puppeteer.use(StealthPlugin());

// ── Config (env vars set by the Python side) ───────────────────────────────
const env = process.env;
const BASE_URL = (env.IPTVV_BASE_URL || 'https://iptvv.ca').replace(/\/+$/, '');
const CART_URL = env.IPTVV_CART_URL || `${BASE_URL}/cart/`;
const CHECKOUT_URL = `${BASE_URL}/checkout/`;
const TRIAL_PRODUCT_ID = env.IPTVV_TRIAL_PRODUCT_ID || '7758';
const PROXY_CHECK_URL = env.IPTVV_PROXY_CHECK_URL || 'https://api.ipify.org';
const CLOUDFLARE_WAIT_MS = parseInt(env.IPTVV_CLOUDFLARE_WAIT_SECONDS || '45', 10) * 1000;
const PAGE_LOAD_RETRIES = parseInt(env.IPTVV_PAGE_LOAD_RETRIES || '2', 10);
const DEBUG_DIR = env.IPTVV_DEBUG_DIR || '/app/logs';
const HEADLESS = (env.HEADLESS || 'False').toLowerCase() === 'true';
const PROXY_SERVER = (env.BROWSER_PROXY_SERVER || '').trim();
const NAV_TIMEOUT_MS = 60000;

const ADD_TO_CART_URL = `${BASE_URL}/?add-to-cart=${TRIAL_PRODUCT_ID}`;

let browser = null;
let page = null;

// ── Helpers ────────────────────────────────────────────────────────────────
const log = (...args) => console.error(...args);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const rand = (min, max) => Math.floor(min + Math.random() * (max - min));

class WorkerError extends Error {
  constructor(type, message, diag) {
    super(message);
    this.type = type;
    this.diag = diag || null;
  }
}

function findChromeBinary() {
  const explicit = (env.CHROME_BINARY || '').trim();
  if (explicit) return explicit;
  for (const candidate of ['/usr/bin/google-chrome', '/usr/bin/google-chrome-stable',
    '/usr/bin/chromium', '/usr/bin/chromium-browser']) {
    if (fs.existsSync(candidate)) return candidate;
  }
  throw new WorkerError('launch_failed', 'No Chrome binary found; set CHROME_BINARY');
}

function requirePage() {
  if (!page) throw new WorkerError('not_launched', 'Browser not launched; send "launch" first');
  return page;
}

async function goto(url) {
  const p = requirePage();
  try {
    await p.goto(url, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT_MS });
  } catch (exc) {
    // Navigation timeouts / net errors still leave a page we can classify.
    log(`[!] Navigation to ${url} raised: ${exc.message}`);
  }
}

async function safeUrl() {
  try { return page ? page.url() : ''; } catch { return ''; }
}

async function safeTitle() {
  try { return page ? await page.title() : ''; } catch { return ''; }
}

async function bodyText() {
  try {
    return await page.evaluate(() => (document.body ? document.body.innerText : '') || '');
  } catch { return ''; }
}

async function pageHtml() {
  try { return await page.content(); } catch { return ''; }
}

async function waitForReadyState(maxMs = 10000) {
  const end = Date.now() + maxMs;
  while (Date.now() < end) {
    try {
      if ((await page.evaluate(() => document.readyState)) === 'complete') return;
    } catch { /* page mid-navigation */ }
    await sleep(500);
  }
}

/** Human-ish idle: small mouse movement + scroll, per the article's behavioral advice. */
async function humanJiggle() {
  try {
    const vp = page.viewport() || { width: 1200, height: 800 };
    await page.mouse.move(rand(50, vp.width - 50), rand(50, vp.height - 50), { steps: rand(5, 15) });
    await page.evaluate((dy) => window.scrollBy(0, dy), rand(-120, 240));
  } catch { /* best effort */ }
}

// ── Page classification (ported from the Selenium helpers) ────────────────
async function isCloudflareBlockPage() {
  const title = (await safeTitle()).toLowerCase();
  const url = (await safeUrl()).toLowerCase();
  const body = (await bodyText()).toLowerCase();
  const markers = [
    'attention required', 'cloudflare', 'checking your browser', 'verify you are human',
    'cf-browser-verification', 'ray id', 'error 1020', 'access denied', 'just a moment',
  ];
  if (title.includes('cloudflare') || title.includes('just a moment') || url.includes('cdn-cgi')) return true;
  if (markers.some((m) => body.includes(m))) return true;
  // Turnstile / managed-challenge iframes (article's detection).
  try {
    const challengeFrame = await page.$(
      'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"], #challenge-form, #cf-challenge-running'
    );
    if (challengeFrame) return true;
  } catch { /* ignore */ }
  return false;
}

async function isBrowserErrorPage() {
  const body = (await bodyText()).toLowerCase();
  const url = (await safeUrl()).toLowerCase();
  const markers = [
    "this site can't be reached", 'this site can’t be reached', 'err_', 'dns_probe_finished',
    'took too long to respond', 'your internet access is blocked', 'no internet',
  ];
  return url.startsWith('chrome-error://') || markers.some((m) => body.includes(m));
}

async function hasBillingEmail() {
  try { return Boolean(await page.$('#billing_email')); } catch { return false; }
}

async function extractCloudflareDiagnostics() {
  const html = await pageHtml();
  const text = html.replace(/<[^>]+>/g, ' ').replace(/&nbsp;/g, ' ').replace(/\s+/g, ' ').trim();
  let rayId = 'unknown';
  const ray = text.match(/Ray ID:?\s*([0-9a-fA-F]{12,})/);
  if (ray) rayId = ray[1];
  let reason = 'Cloudflare/WAF block page';
  for (const re of [/You are unable to access[^.]*\.?/i, /Sorry, you have been blocked\.?/i,
    /Attention Required![^.]*\.?/i, /Access denied\.?/i, /Verify you are human[^.]*\.?/i]) {
    const m = text.match(re);
    if (m) { reason = m[0].trim(); break; }
  }
  return { ray_id: rayId, reason, url: await safeUrl(), title: await safeTitle() };
}

function resolveDebugDir() {
  for (const dir of [DEBUG_DIR, './logs', path.join(require('os').homedir(), 'logs'), '/tmp']) {
    try {
      fs.mkdirSync(dir, { recursive: true });
      const probe = path.join(dir, `.write_test_${process.pid}`);
      fs.writeFileSync(probe, 'test');
      fs.unlinkSync(probe);
      if (dir !== DEBUG_DIR) log(`[*] Using fallback debug directory: ${dir}`);
      return dir;
    } catch { /* try next */ }
  }
  return '/tmp';
}

async function saveDebugArtifacts(label) {
  const artifacts = {};
  if (!page) return artifacts;
  const ts = new Date().toISOString().replace(/[-:]/g, '').replace('T', '_').slice(0, 15);
  const safeLabel = String(label).replace(/[^a-zA-Z0-9_-]+/g, '_').replace(/^_+|_+$/g, '') || 'page';
  const base = path.join(resolveDebugDir(), `iptvv_${safeLabel}_${ts}`);
  try {
    await page.screenshot({ path: `${base}.png`, fullPage: true });
    artifacts.screenshot = `${base}.png`;
    log(`[*] Screenshot saved to: ${base}.png`);
  } catch (exc) { log(`[!] Could not save screenshot: ${exc.message}`); }
  try {
    fs.writeFileSync(`${base}.html`, await pageHtml(), 'utf-8');
    artifacts.html = `${base}.html`;
    log(`[*] HTML snapshot saved to: ${base}.html`);
  } catch (exc) { log(`[!] Could not save HTML snapshot: ${exc.message}`); }
  return artifacts;
}

async function logPageDiagnostics(label) {
  log(`[diag] ===== page diagnostics (${label}) =====`);
  log(`[diag] current_url: ${await safeUrl()}`);
  log(`[diag] title: ${JSON.stringify(await safeTitle())}`);
  const text = await bodyText();
  const html = await pageHtml();
  log(`[diag] body text length: ${text.length}`);
  if (text) log(`[diag] body text preview: ${text.replace(/\s+/g, ' ').slice(0, 500)}`);
  log(`[diag] page_source length: ${html.length}`);
  if (html) log(`[diag] page_source head: ${html.replace(/\s+/g, ' ').slice(0, 1200)}`);
  const lowered = `${text} ${html}`.toLowerCase();
  const classes = [];
  if (['proxy authentication required', 'err_tunnel_connection_failed', 'err_proxy_connection_failed',
    'err_no_supported_proxies'].some((m) => lowered.includes(m))) classes.push('PROXY AUTH / TUNNEL FAILURE');
  if (['just a moment', 'checking your browser', 'cf-challenge', 'challenge-platform']
    .some((m) => lowered.includes(m))) classes.push('CLOUDFLARE JS CHALLENGE (interstitial, not a hard block)');
  if (['your cart is currently empty', 'your cart is empty', 'no products in the cart']
    .some((m) => lowered.includes(m))) classes.push('EMPTY CART (trial product was never added)');
  if ((await safeUrl()).startsWith('chrome-error://')) classes.push('CHROME ERROR PAGE (never reached the site)');
  if (html.length < 200 && text.length < 5) classes.push('ESSENTIALLY EMPTY DOCUMENT (blank response)');
  log(`[diag] classification: ${classes.length ? classes.join(', ') : 'unknown - inspect DEBUG_PAGE'}`);
  log('[diag] ===================================');
}

/**
 * Wait until the WooCommerce checkout form is present, giving Cloudflare
 * challenges time to clear (the stealth Chrome usually passes the managed
 * challenge on its own). Port of wait_for_real_checkout_page().
 */
async function waitForRealCheckoutPage(context) {
  log(`[*] Verifying checkout page after ${context}...`);
  const deadline = Date.now() + CLOUDFLARE_WAIT_MS;
  let cloudflareSeen = false;
  let reloads = 0;
  let challengeSince = null;

  while (Date.now() < deadline) {
    if (await hasBillingEmail()) {
      log('[OK] Billing email field detected - checkout form is loaded');
      return true;
    }
    if (await isCloudflareBlockPage()) {
      cloudflareSeen = true;
      challengeSince = challengeSince || Date.now();
      log('[!] Cloudflare/WAF page detected instead of checkout; waiting for clearance...');
      await humanJiggle();
      // Give the challenge ~15s to auto-resolve before forcing a reload.
      if (Date.now() - challengeSince > 15000 && reloads < PAGE_LOAD_RETRIES) {
        reloads += 1;
        challengeSince = null;
        log(`[*] Reloading page after blocker detection (${reloads}/${PAGE_LOAD_RETRIES})...`);
        try { await page.reload({ waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT_MS }); } catch { /* ignore */ }
      }
      await sleep(3000);
      continue;
    }
    challengeSince = null;
    await sleep(1000);
  }

  if (cloudflareSeen || (await isCloudflareBlockPage())) {
    const artifacts = await saveDebugArtifacts('cloudflare_block');
    const diag = { ...(await extractCloudflareDiagnostics()), artifacts };
    throw new WorkerError('cloudflare_blocked',
      `IPTVV checkout is blocked by Cloudflare/WAF; the checkout form never loaded after ${context}.`, diag);
  }

  const artifacts = await saveDebugArtifacts('checkout_not_loaded');
  await logPageDiagnostics('checkout_not_loaded');
  const text = (await bodyText()).toLowerCase();
  const emptyCart = ['your cart is currently empty', 'your cart is empty', 'no products in the cart',
    'cart is currently empty'].some((m) => text.includes(m));
  const type = (await isBrowserErrorPage()) ? 'network_error' : 'checkout_not_loaded';
  throw new WorkerError(type,
    `IPTVV checkout form did not load after ${context}` +
    (emptyCart ? ' (page shows an empty cart - the trial product was never added)' : ''),
    { url: await safeUrl(), title: await safeTitle(), artifacts, empty_cart: emptyCart });
}

// ── In-page helpers ────────────────────────────────────────────────────────
/** Mark the first visible element matching `selector`/text terms with a data attribute; return its handle. */
async function findVisible(selector, terms = null) {
  const handle = await page.evaluateHandle((sel, termList) => {
    const visible = (el) => {
      const r = el.getBoundingClientRect();
      const s = window.getComputedStyle(el);
      return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && !el.disabled;
    };
    for (const el of document.querySelectorAll(sel)) {
      if (!visible(el)) continue;
      if (!termList) return el;
      const text = [el.innerText, el.value, el.getAttribute('title'), el.getAttribute('aria-label')]
        .filter(Boolean).join(' ').toLowerCase();
      if (termList.some((t) => text.includes(t))) return el;
    }
    return null;
  }, selector, terms ? terms.map((t) => t.toLowerCase()) : null);
  const el = handle.asElement();
  if (!el) { await handle.dispose(); return null; }
  return el;
}

async function pollFor(fn, timeoutMs, intervalMs = 500) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    const res = await fn();
    if (res) return res;
    await sleep(intervalMs);
  }
  return null;
}

async function safeClick(el) {
  try {
    await el.evaluate((n) => n.scrollIntoView({ block: 'center' }));
    await sleep(rand(250, 600));
    await el.click({ delay: rand(30, 120) });
  } catch {
    await el.evaluate((n) => n.click());
  }
}

async function labelTextFor(el) {
  return el.evaluate((n) => {
    let label = null;
    if (n.id) label = document.querySelector(`label[for="${CSS.escape(n.id)}"]`);
    const src = label || n.closest('label') || n.parentElement;
    return ((src && src.innerText) || '').toLowerCase();
  });
}

// ── Commands ───────────────────────────────────────────────────────────────
async function cmdLaunch() {
  if (browser) return { already: true };
  const executablePath = findChromeBinary();
  const args = [
    '--no-sandbox',
    '--disable-dev-shm-usage',
    '--disable-gpu',
    '--window-size=1920,1080',
    '--disable-blink-features=AutomationControlled',
    '--disable-popup-blocking',
    '--disable-notifications',
    '--ignore-certificate-errors',
  ];
  if (PROXY_SERVER) {
    // Python's local forwarding proxy (no auth) -> upstream residential proxy.
    args.push(`--proxy-server=${PROXY_SERVER}`, '--proxy-bypass-list=<-loopback>');
    log(`[*] Routing browser through ${PROXY_SERVER}`);
  } else {
    args.push('--no-proxy-server');
    log('[*] Using direct connection (no proxy)');
  }
  log(`[*] Launching Chrome (${HEADLESS ? 'headless' : 'headed'}) via puppeteer-extra stealth: ${executablePath}`);
  try {
    browser = await puppeteer.launch({
      executablePath,
      headless: HEADLESS,
      args,
      defaultViewport: null,
      ignoreDefaultArgs: ['--enable-automation'],
    });
  } catch (exc) {
    throw new WorkerError('launch_failed', `Failed to launch Chrome: ${exc.message}`);
  }
  const pages = await browser.pages();
  page = pages[0] || (await browser.newPage());
  // Randomized viewport (article recommendation) to avoid a fixed fingerprint.
  await page.setViewport({ width: rand(1280, 1440), height: rand(800, 900) });
  page.setDefaultTimeout(30000);
  page.setDefaultNavigationTimeout(NAV_TIMEOUT_MS);
  const version = await browser.version();
  log(`[OK] Browser ready: ${version}`);
  return { version };
}

async function cmdBrowserIp() {
  requirePage();
  const original = await safeUrl();
  try {
    await goto(PROXY_CHECK_URL);
    const ip = (await bodyText()).replace(/\s+/g, ' ').trim();
    return ip || 'unknown';
  } catch (exc) {
    log(`[!] Could not fetch browser public IP from ${PROXY_CHECK_URL}: ${exc.message}`);
    return 'unknown';
  } finally {
    if (original && original.startsWith('http')) await goto(original);
  }
}

async function cartHasItems() {
  await goto(CART_URL);
  await sleep(2000);
  if (await page.$('.cart_item')) return true;
  if ((await bodyText()).toLowerCase().includes('your cart is currently empty')) return false;
  return true; // Unknown layout: let checkout verification decide.
}

async function openCheckoutAndVerify(context) {
  log(`[*] Navigating to checkout: ${CHECKOUT_URL}`);
  await goto(CHECKOUT_URL);
  await sleep(rand(2500, 4500));
  await waitForReadyState();
  log(`[*] Checkout URL: ${await safeUrl()}`);
  log(`[*] Page title: ${await safeTitle()}`);
  await waitForRealCheckoutPage(context);
}

async function selectFullChannelPackage() {
  log("[*] Looking for 'full channel' option...");

  // Strategy 1: <select> dropdowns.
  const selected = await page.evaluate(() => {
    for (const sel of document.querySelectorAll('select')) {
      const opts = Array.from(sel.options);
      const all = opts.map((o) => o.text.toLowerCase()).join(' ');
      if (!/channel|package|plan/.test(all)) continue;
      const pick = opts.find((o) => o.text.toLowerCase().includes('full') && o.text.toLowerCase().includes('channel'))
        || opts.find((o) => /all|full/.test(o.text.toLowerCase()));
      if (pick) {
        sel.value = pick.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        return pick.text;
      }
    }
    return null;
  });
  if (selected) { log(`[OK] Selected: ${selected}`); return true; }

  // Strategies 2 & 3: radio buttons, then checkboxes.
  for (const type of ['radio', 'checkbox']) {
    for (const el of await page.$$(`input[type="${type}"]`)) {
      const label = await labelTextFor(el);
      if (label.includes('full') && label.includes('channel')) {
        const checked = await el.evaluate((n) => n.checked);
        if (!checked) await safeClick(el);
        log(`[OK] Selected ${type}: ${label.trim()}`);
        return true;
      }
    }
  }
  log("[*] Could not find 'full channel' option - may not be required or already selected");
  return false;
}

async function cmdOpenTrialCheckout() {
  requirePage();
  log(`[*] Navigating to IPTVV cart: ${CART_URL}`);
  await goto(CART_URL);
  await sleep(2000);
  log(`[*] Current URL: ${await safeUrl()}`);

  // A Cloudflare challenge on the very first page: wait it out before looking for buttons.
  if (await isCloudflareBlockPage()) {
    log('[!] Cloudflare challenge on cart page; waiting for clearance...');
    await pollFor(async () => !(await isCloudflareBlockPage()), CLOUDFLARE_WAIT_MS, 2000);
  }

  log('[*] Looking for the add-to-cart trial button...');
  let button = await pollFor(() => findVisible("a[href*='add-to-cart']"), 10000);
  if (!button) {
    button = await pollFor(() => findVisible(
      "a, button, input[type='submit'], input[type='button'], label, [role='button']",
      ['start my free trial', 'start free trial', 'add to cart']), 10000);
  }
  if (button) {
    const label = await button.evaluate((n) => n.innerText || n.getAttribute('href') || '');
    log(`[OK] Found element: ${label.trim()}`);
    await safeClick(button);
    log('[OK] Clicked trial button - adding product to cart...');
    await sleep(5000);
    log(`[*] After click URL: ${await safeUrl()}`);
  } else {
    log('[!] Add-to-cart trial button not found');
    await logPageDiagnostics('cart_button_not_found');
  }

  if (!(await cartHasItems())) {
    log('[*] Cart is still empty; trying direct add-to-cart URL...');
    await goto(ADD_TO_CART_URL);
    await sleep(8000);
    log(`[*] After add-to-cart URL: ${await safeUrl()}`);
    if (!(await cartHasItems())) log('[!] Cart still looks empty after direct add-to-cart; continuing to checkout anyway');
  }

  try {
    await openCheckoutAndVerify('free-trial cart flow');
  } catch (exc) {
    if (exc.type === 'cloudflare_blocked' || !(await safeUrl()).includes('/cart')) throw exc;
    log('[!] Checkout bounced back to the cart; re-adding trial product and retrying once...');
    await goto(ADD_TO_CART_URL);
    await sleep(8000);
    await openCheckoutAndVerify('direct add-to-cart retry');
  }

  await selectFullChannelPackage();
  return { url: await safeUrl() };
}

async function cmdPreflight() {
  requirePage();
  log('[*] Seeding free-trial cart for checkout preflight...');
  await goto(ADD_TO_CART_URL);
  await sleep(5000);
  log(`[*] Opening checkout: ${CHECKOUT_URL}`);
  await goto(CHECKOUT_URL);
  await sleep(5000);

  // Give a Cloudflare challenge a chance to clear before classifying.
  if (await isCloudflareBlockPage()) {
    log('[!] Cloudflare challenge detected; waiting for clearance...');
    await pollFor(async () => (await hasBillingEmail()) || !(await isCloudflareBlockPage()), CLOUDFLARE_WAIT_MS, 2000);
  }

  const artifacts = await saveDebugArtifacts('preflight_checkout');
  const url = await safeUrl();
  const title = await safeTitle();
  log(`[*] Checkout URL after load: ${url}`);
  log(`[*] Page title: ${title}`);

  if (await hasBillingEmail()) return { url, title, artifacts };
  if (await isCloudflareBlockPage()) {
    throw new WorkerError('cloudflare_blocked', 'Preflight failed: IPTVV checkout is blocked by Cloudflare/WAF.',
      { ...(await extractCloudflareDiagnostics()), artifacts });
  }
  if (await isBrowserErrorPage()) {
    throw new WorkerError('network_error',
      'Preflight failed: network/connectivity error - Chrome never reached IPTVV '
      + '(this is NOT a Cloudflare IP block; check DNS/egress/proxy settings).', { url, title, artifacts });
  }
  throw new WorkerError('checkout_not_loaded',
    'Preflight failed: no Cloudflare block and no network error detected, '
    + 'but the checkout billing_email field is missing (unexpected page layout).', { url, title, artifacts });
}

/** Return a visible, enabled field by id then name, trying each candidate in order. */
async function findField(names) {
  for (const name of names) {
    for (const sel of [`#${name}`, `[name="${name}"]`]) {
      let el;
      try { el = await page.$(sel); } catch { el = null; }
      if (!el) continue;
      const usable = await el.evaluate((n) => {
        const r = n.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && !n.disabled;
      });
      if (usable) return { el, name };
    }
  }
  return null;
}

async function typeInto(el, value) {
  await el.evaluate((n) => n.scrollIntoView({ block: 'center' }));
  await sleep(rand(150, 400));
  await el.click({ clickCount: 3 });
  await el.evaluate((n) => { n.value = ''; });
  await el.type(String(value), { delay: rand(25, 70) });
}

async function setSelectValue(id, predicate) {
  return page.evaluate((selId, predSrc) => {
    const sel = document.getElementById(selId) || document.querySelector(`[name="${selId}"]`);
    if (!sel || sel.tagName !== 'SELECT') return null;
    // eslint-disable-next-line no-new-func
    const pred = new Function('o', `return (${predSrc})(o);`);
    const opt = Array.from(sel.options).find((o) => pred(o));
    if (!opt) return null;
    sel.value = opt.value;
    sel.dispatchEvent(new Event('change', { bubbles: true }));
    // select2 / jQuery listeners (WooCommerce country/state).
    if (window.jQuery) window.jQuery(sel).trigger('change');
    return opt.text;
  }, id, predicate.toString());
}

async function cmdFill({ email, user_data: userData }) {
  requirePage();
  if (!email) throw new WorkerError('bad_request', 'fill requires "email"');
  log('[*] Filling WooCommerce checkout form...');
  await waitForRealCheckoutPage('form fill step');
  await sleep(2000);

  const data = { ...(userData || {}), email };
  const mappings = {
    email: ['billing_email', 'email'],
    first_name: ['billing_first_name', 'firstname', 'first_name'],
    last_name: ['billing_last_name', 'lastname', 'last_name'],
    phone: ['billing_phone', 'phone'],
    address: ['billing_address_1', 'address', 'address1'],
    city: ['billing_city', 'city'],
    postal_code: ['billing_postcode', 'postal', 'postcode', 'zip'],
  };
  const filled = [];

  // Country first: WooCommerce re-renders state/postcode fields on country change.
  const country = await setSelectValue('billing_country',
    (o) => o.value.toLowerCase() === 'ca' || o.text.toLowerCase().includes('canada'));
  if (country) { log('[OK] Selected country: Canada'); filled.push('billing_country'); await sleep(1500); } else {
    log('[!] Could not find country dropdown');
  }

  for (const [key, names] of Object.entries(mappings)) {
    const value = data[key];
    if (!value) continue;
    const found = await findField(names);
    if (!found) { log(`[!] Could not find field for: ${key}`); continue; }
    try {
      await typeInto(found.el, value);
      log(`[OK] Filled ${found.name}: ${value}`);
      filled.push(found.name);
    } catch (exc) {
      log(`[!] Could not fill ${found.name}: ${exc.message}`);
    }
  }

  const state = await setSelectValue('billing_state', (o) => o.value === 'ON');
  if (state) { log('[OK] Selected state: Ontario'); filled.push('billing_state'); }

  // IPTVV custom required field: device_select[] checkboxes.
  log('[*] Looking for device selection checkboxes...');
  const devices = await page.$$('[name="device_select[]"]');
  if (devices.length) {
    log(`[*] Found ${devices.length} device checkboxes`);
    let target = null;
    for (const cb of devices) {
      const value = await cb.evaluate((n) => n.value || '');
      if (['androidbox', 'smarttv', 'firetv'].includes(value)) { target = cb; break; }
    }
    target = target || devices[0];
    const [value, checked] = await target.evaluate((n) => [n.value || 'first', n.checked]);
    if (!checked) await safeClick(target);
    log(`[OK] Selected device: ${value}`);
    filled.push('device_select');
  } else {
    log("[!] No device checkboxes found with name='device_select[]'");
  }

  // IPTVV custom required field: channel packages (checkbox or select).
  const pkgBoxes = await page.$$(
    "input[type='checkbox'][id*='channel'], input[type='checkbox'][name*='channel'], "
    + "input[type='checkbox'][id*='package'], input[type='checkbox'][name*='package']");
  if (pkgBoxes.length) {
    const checked = await pkgBoxes[0].evaluate((n) => n.checked);
    if (!checked) await safeClick(pkgBoxes[0]);
    log('[OK] Selected channel package checkbox');
    filled.push('billing_channel_packages');
  } else {
    for (const id of ['billing_channel_packages', 'billing_packages', 'channel_packages', 'packages']) {
      const picked = await page.evaluate((selId) => {
        const sel = document.getElementById(selId);
        if (!sel || sel.tagName !== 'SELECT') return null;
        const opts = Array.from(sel.options);
        const opt = opts.find((o) => /full|all/.test(o.text.toLowerCase())) || opts[1];
        if (!opt) return null;
        sel.value = opt.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        return opt.text;
      }, id);
      if (picked) { log(`[OK] Selected package: ${picked}`); filled.push('billing_channel_packages'); break; }
    }
  }

  log(`[OK] Filled ${filled.length} fields: ${filled.join(', ')}`);
  log(`[*] Using email: ${email}`);
  return { filled };
}

async function cmdCaptchaInfo() {
  requirePage();
  const siteKey = await page.evaluate(() => {
    for (const f of document.querySelectorAll('iframe')) {
      const src = f.getAttribute('src') || '';
      const m = src.includes('recaptcha') && src.includes('api2/anchor') && src.match(/[?&]k=([^&]+)/);
      if (m) return m[1];
    }
    const el = document.querySelector('.g-recaptcha[data-sitekey]');
    return el ? el.getAttribute('data-sitekey') : null;
  });
  return siteKey ? { sitekey: siteKey, url: await safeUrl() } : null;
}

async function injectRecaptchaToken(token) {
  await page.evaluate((tok) => {
    const ta = document.getElementById('g-recaptcha-response');
    if (ta) {
      ta.value = tok;
      ta.innerHTML = tok;
      ta.dispatchEvent(new Event('change', { bubbles: true }));
    }
    const el = document.querySelector('.g-recaptcha');
    if (el) {
      const cb = el.getAttribute('data-callback');
      if (cb && typeof window[cb] === 'function') window[cb](tok);
    }
  }, token);
  log('[OK] reCAPTCHA token injected');
  await sleep(2000);
}

async function cmdSubmit({ token } = {}) {
  requirePage();
  log('[*] Submitting WooCommerce checkout form...');
  await waitForRealCheckoutPage('submit step');
  if (token) await injectRecaptchaToken(token);

  let button = await findVisible('#place_order');
  if (button) {
    log('[OK] Found WooCommerce place_order button');
  } else {
    button = await pollFor(() => findVisible(
      "a, button, input[type='submit'], input[type='button'], [role='button']",
      ['place order', 'place trial order', 'submit', 'checkout', 'complete order']), 15000);
    if (!button) throw new WorkerError('submit_not_found', 'Could not find or click submit button');
    log('[OK] Found submit button by text');
  }

  await safeClick(button);
  log('[OK] Submit button clicked');
  log('[*] Waiting for order processing...');
  // WooCommerce submits via AJAX then redirects; wait for either the redirect or ~15s.
  await pollFor(async () => /order-received|thank-you/.test(await safeUrl()), 15000, 1000);
  await sleep(2000);
  const url = await safeUrl();
  log(`[*] After submit URL: ${url}`);

  if (/order-received|thank-you/.test(url)) {
    log('[OK] Order successfully submitted! (on thank you page)');
    return { outcome: 'submitted', url, errors: [] };
  }

  if (!url.includes('checkout')) return { outcome: 'unknown', url, errors: [] };

  log('[!] WARNING: Still on checkout page - order submission failed');
  const artifacts = await saveDebugArtifacts('checkout_error');
  const { errors, invalidFields, text } = await page.evaluate(() => ({
    errors: Array.from(document.querySelectorAll(
      '.woocommerce-error li, .woocommerce-NoticeGroup-checkout li, .woocommerce-error'))
      .map((e) => e.innerText.trim()).filter(Boolean),
    invalidFields: Array.from(document.querySelectorAll('.woocommerce-invalid-required-field, [aria-invalid="true"]'))
      .map((e) => e.getAttribute('name') || e.id || 'unknown'),
    text: (document.body && document.body.innerText) || '',
  }));
  errors.forEach((e, i) => log(`    Error ${i + 1}: ${e}`));
  if (invalidFields.length) log(`[!] Found ${invalidFields.length} invalid/required fields: ${invalidFields.join(', ')}`);

  const lower = text.toLowerCase();
  const outcome = (lower.includes('disposable email') || lower.includes('temporary or disposable'))
    ? 'disposable_rejected' : 'still_checkout';
  return { outcome, url, errors, invalid_fields: invalidFields, artifacts };
}

async function cmdClose() {
  if (browser) {
    try { await browser.close(); } catch { /* ignore */ }
  }
  browser = null;
  page = null;
  return { closed: true };
}

const COMMANDS = {
  launch: cmdLaunch,
  browser_ip: cmdBrowserIp,
  open_trial_checkout: cmdOpenTrialCheckout,
  preflight: cmdPreflight,
  fill: cmdFill,
  captcha_info: cmdCaptchaInfo,
  submit: cmdSubmit,
  close: cmdClose,
};

// ── JSON-lines loop ────────────────────────────────────────────────────────
function reply(obj) {
  process.stdout.write(`${JSON.stringify(obj)}\n`);
}

async function handle(line) {
  let msg;
  try { msg = JSON.parse(line); } catch {
    reply({ id: null, ok: false, error: { type: 'bad_request', message: `Invalid JSON: ${line.slice(0, 200)}` } });
    return;
  }
  const { id = null, cmd, args = {} } = msg;
  const fn = COMMANDS[cmd];
  if (!fn) {
    reply({ id, ok: false, error: { type: 'bad_request', message: `Unknown command: ${cmd}` } });
    return;
  }
  try {
    reply({ id, ok: true, result: (await fn(args)) ?? null });
  } catch (exc) {
    reply({
      id,
      ok: false,
      error: { type: exc.type || 'error', message: exc.message || String(exc), diag: exc.diag || null },
    });
  }
}

async function main() {
  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  // Commands are processed strictly in order.
  for await (const line of rl) {
    if (line.trim()) await handle(line);
  }
  await cmdClose();
}

const shutdown = async () => { await cmdClose(); process.exit(0); };
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);

if (require.main === module) {
  main().catch(async (exc) => {
    log(`[!] Worker crashed: ${exc.stack || exc}`);
    await cmdClose();
    process.exit(1);
  });
}

module.exports = { COMMANDS };
