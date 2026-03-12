"""
scraper.py  –  Selenium scrapers for Blinkit, Zepto, BigBasket with product URLs
Optimised for speed: batch DOM extraction, minimal sleeps, JS-based scroll+extract.
"""

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

# ── Chrome options ────────────────────────────────────────────────────────────
def _make_options() -> Options:
    import tempfile, os
    
    # Use /tmp for Chrome data — avoids /dev/shm limitations on Render
    tmp_dir = tempfile.mkdtemp()
    
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-setuid-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--disable-software-rasterizer")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-background-timer-throttling")
    opts.add_argument("--disable-backgrounding-occluded-windows")
    opts.add_argument("--disable-breakpad")
    opts.add_argument("--disable-client-side-phishing-detection")
    opts.add_argument("--disable-component-update")
    opts.add_argument("--disable-default-apps")
    opts.add_argument("--disable-domain-reliability")
    opts.add_argument("--disable-features=AudioServiceOutOfProcess")
    opts.add_argument("--disable-hang-monitor")
    opts.add_argument("--disable-ipc-flooding-protection")
    opts.add_argument("--disable-popup-blocking")
    opts.add_argument("--disable-prompt-on-repost")
    opts.add_argument("--disable-renderer-backgrounding")
    opts.add_argument("--disable-sync")
    opts.add_argument("--disable-translate")
    opts.add_argument("--force-color-profile=srgb")
    opts.add_argument("--hide-scrollbars")
    opts.add_argument("--metrics-recording-only")
    opts.add_argument("--mute-audio")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-zygote")           # ← key flag for low RAM containers
    opts.add_argument("--single-process")      # ← runs Chrome in single process
    opts.add_argument("--safebrowsing-disable-auto-update")
    opts.add_argument("--window-size=1280,720")
    opts.add_argument(f"--disk-cache-dir={tmp_dir}")
    opts.add_argument(f"--user-data-dir={tmp_dir}")
    opts.add_argument("--js-flags=--max-old-space-size=128")
    opts.add_experimental_option("prefs", {
        "profile.default_content_setting_values.notifications": 2,
        "profile.managed_default_content_settings.stylesheets": 2,
        "profile.managed_default_content_settings.images": 2,
        "profile.managed_default_content_settings.javascript": 1,
    })
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    return opts

BLINKIT_BASE   = "https://blinkit.com"
ZEPTO_BASE     = "https://www.zeptonow.com"
BIGBASKET_BASE = "https://www.bigbasket.com"

_PAGE_LOAD_TIMEOUT = 12   # seconds for WebDriverWait


# ── Helpers ──────────────────────────────────────────────────────────────────

def _chrome() -> webdriver.Chrome:
    import tempfile, shutil
    tmp_dir = tempfile.mkdtemp()
    opts = _make_options()
    # Override with fresh unique dir per instance
    opts.add_argument(f"--disk-cache-dir={tmp_dir}")
    opts.add_argument(f"--user-data-dir={tmp_dir}")
    driver = webdriver.Chrome(options=opts)
    driver._tmp_dir = tmp_dir  # store for cleanup
    return driver


def _wait_for_cards(driver, css: str, timeout: float = _PAGE_LOAD_TIMEOUT) -> list:
    """Block until at least one card is present, then return all."""
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, css))
        )
    except Exception:
        pass
    return driver.find_elements(By.CSS_SELECTOR, css)


def _scroll_all_cards(driver, cards: list):
    """
    Scroll all cards into view in ONE JS call instead of N individual scrolls.
    Single consolidated sleep afterwards lets lazy images start loading.
    """
    driver.execute_script(
        "arguments[0].forEach(el => el.scrollIntoView({block:'center'}));",
        cards,
    )
    time.sleep(1.2)


def _wait_img_src(driver, img_el, timeout: float = 4) -> str:
    """Wait for a real (non-placeholder) src on an already-visible img element."""
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: (
                img_el.get_attribute("src")
                and img_el.get_attribute("src").startswith("http")
                and "placeholder" not in img_el.get_attribute("src").lower()
                and "data:image" not in img_el.get_attribute("src")
            )
        )
        return img_el.get_attribute("src")
    except Exception:
        src = img_el.get_attribute("src") or ""
        return src if src.startswith("http") else "N/A"


def _resolve_img(driver, img_elements: list, idx: int, initial_src: str) -> str:
    """Return image URL; wait for lazy-load only if needed."""
    src = initial_src or ""
    good = src.startswith("http") and "placeholder" not in src.lower() and "data:image" not in src
    if good:
        return src
    img_el = img_elements[idx] if idx < len(img_elements) else None
    if img_el:
        return _wait_img_src(driver, img_el, timeout=3)
    return "N/A"


def _build_url(href: str, base: str, fallback: str) -> str:
    if not href:
        return fallback
    href = href.split("?")[0]
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return base + href
    return fallback


# ── Blinkit ──────────────────────────────────────────────────────────────────

def scrape_blinkit(item: str, location: str) -> list[dict]:
    driver = _chrome()
    results = []
    fallback_url = f"{BLINKIT_BASE}/s/?q={item}"
    try:
        driver.get(fallback_url)
        cards = _wait_for_cards(
            driver, "div[id][role='button'][style*='border-radius: 8px']"
        )
        print(f"[Blinkit] {len(cards)} cards")
        if not cards:
            return results

        _scroll_all_cards(driver, cards)

        # Single JS round-trip extracts all fields + img src
        data = driver.execute_script("""
            return arguments[0].map(el => {
                const name = el.querySelector("div.tw-line-clamp-2");
                const qty  = el.querySelector("div[style*='color: var(--colors-grey-700)']");
                const sp   = el.querySelector("div.tw-text-200.tw-font-semibold");
                const mrp  = el.querySelector("div[style*='color: var(--colors-grey-600)'] span span");
                const disc = el.querySelector("div[style*='color: var(--colors-white-900)']");
                const img  = el.querySelector("div[style*='aspect-ratio'] img");
                return {
                    id:            el.id || '',
                    name:          name ? name.innerText.trim() : 'N/A',
                    quantity:      qty  ? qty.innerText.trim()  : 'N/A',
                    selling_price: sp   ? sp.innerText.trim()   : 'N/A',
                    mrp:           mrp  ? mrp.innerText.trim()  : 'N/A',
                    discount:      disc ? disc.innerText.trim() : 'N/A',
                    img_src:       img  ? (img.src || img.getAttribute('data-src') || '') : '',
                };
            });
        """, cards)

        img_elements = driver.execute_script(
            "return arguments[0].map(el => el.querySelector('div[style*=\"aspect-ratio\"] img') || null);",
            cards,
        )

        for i, row in enumerate(data):
            name = row.get("name", "N/A")
            if not name or name == "N/A":
                continue
            pid = row.get("id", "")
            product_url = (
                f"{BLINKIT_BASE}/prn/product-slug/prid/{pid}" if pid else fallback_url
            )
            results.append({
                "name":          name,
                "quantity":      row.get("quantity",      "N/A"),
                "selling_price": row.get("selling_price", "N/A"),
                "mrp":           row.get("mrp",           "N/A"),
                "discount":      row.get("discount",      "N/A"),
                "image_url":     _resolve_img(driver, img_elements, i, row.get("img_src", "")),
                "product_url":   product_url,
                "source":        "blinkit",
            })
    except Exception as e:
        print(f"[Blinkit] fatal: {e}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        try:
            import shutil
            if hasattr(driver, '_tmp_dir'):
                shutil.rmtree(driver._tmp_dir, ignore_errors=True)
        except Exception:
            pass
    return results


# ── Zepto ────────────────────────────────────────────────────────────────────

def scrape_zepto(item: str, location: str) -> list[dict]:
    driver = _chrome()
    results = []
    fallback_url = f"{ZEPTO_BASE}/search?query={item}"
    try:
        driver.get(fallback_url)
        cards = _wait_for_cards(driver, "a.B4vNQ")
        print(f"[Zepto] {len(cards)} cards")
        if not cards:
            return results

        _scroll_all_cards(driver, cards)

        data = driver.execute_script("""
            return arguments[0].map(el => {
                const name = el.querySelector("div[data-slot-id='ProductName']");
                const qty  = el.querySelector("div[data-slot-id='PackSize'] span");
                const disc = el.querySelector("div.cYCsFo");
                const pc   = el.querySelector("div[data-slot-id='EdlpPrice']");
                const img  = el.querySelector("img");
                let sp = 'N/A', mrp = 'N/A';
                if (pc) {
                    const spans = pc.querySelectorAll('span');
                    if (spans[0]) sp  = spans[0].innerText.trim();
                    if (spans[1]) mrp = spans[1].innerText.trim();
                }
                return {
                    name:          name ? (name.innerText || name.textContent || '').trim() : 'N/A',
                    quantity:      qty  ? qty.innerText.trim()  : 'N/A',
                    discount:      disc ? disc.innerText.trim() : 'N/A',
                    selling_price: sp,
                    mrp:           mrp,
                    href:          el.getAttribute('href') || '',
                    img_src:       img ? (img.src || img.getAttribute('data-src') || '') : '',
                };
            });
        """, cards)

        img_elements = driver.execute_script(
            "return arguments[0].map(el => el.querySelector('img') || null);", cards
        )

        for i, row in enumerate(data):
            name = row.get("name", "N/A")
            if not name or name == "N/A":
                continue
            results.append({
                "name":          name,
                "quantity":      row.get("quantity",      "N/A"),
                "selling_price": row.get("selling_price", "N/A"),
                "mrp":           row.get("mrp",           "N/A"),
                "discount":      row.get("discount",      "N/A"),
                "image_url":     _resolve_img(driver, img_elements, i, row.get("img_src", "")),
                "product_url":   _build_url(row.get("href", ""), ZEPTO_BASE, fallback_url),
                "source":        "zepto",
            })
    except Exception as e:
        print(f"[Zepto] fatal: {e}")
    finally:
        driver.quit()
    return results


# ── BigBasket ────────────────────────────────────────────────────────────────
# IMPORTANT: CSS class "py-1.5" contains a literal dot.
# In a JS querySelector string the dot must be escaped as \\.
# Python raw-string r"..." means \\ becomes a literal two-char sequence \\ in
# the JS source, which the CSS parser interprets as an escaped dot — correct.

_BB_DATA_JS = r"""
return arguments[0].map((card, idx) => {
    const href = arguments[1][idx];

    // Walk up to nearest <li> ancestor (max 8 hops)
    let li = card;
    for (let i = 0; i < 8; i++) {
        if (!li.parentElement) break;
        li = li.parentElement;
        if (li.tagName === 'LI') break;
    }

    const brand = li.querySelector("span.BrandName___StyledLabel2-sc-hssfrl-0");
    const h3    = li.querySelector("div.break-words h3");
    const qty1  = li.querySelector("div[id^='headlessui-listbox-button'] button span");

    // py-1.5 has a literal dot in the class name — escape it for querySelector
    let qty2 = null;
    try { qty2 = li.querySelector("div.py-1\\.5 span span"); } catch(e) {}

    const disc = li.querySelector(
        "div.Offers___StyledDiv-sc-118xvhp-0 span.font-semibold"
    );
    const img = li.querySelector("a img");

    // Price: first div whose direct <span> children start with the rupee sign
    let sp = 'N/A', mrp = 'N/A';
    const allDivs = li.querySelectorAll('div');
    for (const div of allDivs) {
        const spans = div.querySelectorAll(':scope > span');
        if (spans.length >= 1 && spans[0].textContent.includes('\u20b9')) {
            sp  = spans[0].textContent.trim();
            mrp = spans.length > 1 ? spans[1].textContent.trim() : 'N/A';
            break;
        }
    }

    return {
        name:          ((brand ? brand.innerText.trim() : '') + ' ' +
                        (h3 ? (h3.innerText || h3.textContent || '').trim() : 'N/A')).trim() || 'N/A',
        quantity:      qty1 ? qty1.innerText.trim() : (qty2 ? qty2.innerText.trim() : 'N/A'),
        discount:      disc ? disc.innerText.trim() : 'N/A',
        selling_price: sp,
        mrp:           mrp,
        href:          href,
        img_src:       img ? (img.src || img.getAttribute('data-src') || '') : '',
    };
});
"""

_BB_IMG_JS = r"""
return arguments[0].map(card => {
    let li = card;
    for (let i = 0; i < 8; i++) {
        if (!li.parentElement) break;
        li = li.parentElement;
        if (li.tagName === 'LI') break;
    }
    return li.querySelector('a img') || null;
});
"""


def scrape_big_basket(item: str, location: str) -> list[dict]:
    driver = _chrome()
    results = []
    fallback_url = f"{BIGBASKET_BASE}/ps/?q={item}"
    try:
        driver.get(fallback_url)
        cards = _wait_for_cards(driver, "a[href*='/pd/']")
        print(f"[BigBasket] {len(cards)} anchor tags with /pd/")
        if not cards:
            return results

        # Deduplicate by href (image <a> and name <a> share the same href)
        seen: set[str] = set()
        unique: list[tuple] = []
        for c in cards:
            href = (c.get_attribute("href") or "").split("?")[0]
            if href and href not in seen:
                seen.add(href)
                unique.append((c, href))

        print(f"[BigBasket] {len(unique)} unique products")
        unique_cards = [c for c, _ in unique]
        unique_hrefs = [h for _, h in unique]

        _scroll_all_cards(driver, unique_cards)

        data         = driver.execute_script(_BB_DATA_JS, unique_cards, unique_hrefs)
        img_elements = driver.execute_script(_BB_IMG_JS,  unique_cards)

        for i, row in enumerate(data):
            name = row.get("name", "N/A")
            if not name or name == "N/A":
                continue
            results.append({
                "name":          name,
                "quantity":      row.get("quantity",      "N/A"),
                "selling_price": row.get("selling_price", "N/A"),
                "mrp":           row.get("mrp",           "N/A"),
                "discount":      row.get("discount",      "N/A"),
                "image_url":     _resolve_img(driver, img_elements, i, row.get("img_src", "")),
                "product_url":   _build_url(row.get("href", ""), BIGBASKET_BASE, fallback_url),
                "source":        "bigbasket",
            })
    except Exception as e:
        print(f"[BigBasket] fatal: {e}")
    finally:
        driver.quit()
    return results


# ── Parallel entry point ──────────────────────────────────────────────────────

def scrape_all(item: str, location: str) -> tuple[list, list, list]:
    """Run all three scrapers concurrently. Returns (blinkit, zepto, bigbasket)."""
    scrapers = {
        "blinkit":   (scrape_blinkit,    item, location),
        "zepto":     (scrape_zepto,      item, location),
        "bigbasket": (scrape_big_basket, item, location),
    }
    store = {"blinkit": [], "zepto": [], "bigbasket": []}

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(fn, *args): name
            for name, (fn, *args) in scrapers.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                store[name] = future.result()
            except Exception as e:
                print(f"[scrape_all] {name} failed: {e}")
                store[name] = []

    return (store["blinkit"], store["zepto"], store["bigbasket"])