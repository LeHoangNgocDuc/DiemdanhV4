# -*- coding: utf-8 -*-
"""Zalo Web gateway qua Selenium - V16 ổn định/ưu tiên gửi an toàn.

Các nguyên tắc:
1) Không gửi nhầm vào cuộc trò chuyện đang mở trước đó.
2) Không retry mù sau khi đã kích hoạt gửi.
3) Ưu tiên tìm người đã là bạn bằng SĐT đang hiển thị trong danh sách chat.
4) Nếu tìm theo SĐT không được, mới fallback sang Tên Zalo đã lưu và chỉ chấp nhận tên khớp chính xác.
5) Nếu không có: Global Search -> Danh bạ.
6) Nếu không gửi được vì không tìm/không xác nhận được người nhận,
   tự động gửi cảnh báo tới ZALO_FAILURE_NOTIFY_PHONE.
"""

import hashlib
import hmac
import json
import sqlite3
import os
import re
import threading
import time
import traceback
import unicodedata
from datetime import datetime

from flask import Flask, jsonify, request
from flask_cors import CORS
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


# -----------------------------------------------------------------------------
# CONFIG
# -----------------------------------------------------------------------------
app = Flask(__name__)
CORS(app, origins=[])

selenium_lock = threading.RLock()

ZALO_LOGGED_IN_PHONE = os.getenv("ZALO_LOGGED_IN_PHONE", "0972466347")
ZALO_FAILURE_NOTIFY_PHONE = os.getenv("ZALO_FAILURE_NOTIFY_PHONE", "0986108104")

DEDUPE_SECONDS = int(os.getenv("ZALO_DEDUPE_SECONDS", "300"))
DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zalo_debug")

SEARCH_BOX_TIMEOUT = float(os.getenv("ZALO_SEARCH_BOX_TIMEOUT", "1.4"))
SEARCH_RESULT_TIMEOUT = float(os.getenv("ZALO_SEARCH_RESULT_TIMEOUT", "2.8"))
CHAT_INPUT_TIMEOUT = float(os.getenv("ZALO_CHAT_INPUT_TIMEOUT", "4.5"))
RECIPIENT_CONFIRM_TIMEOUT = float(os.getenv("ZALO_RECIPIENT_CONFIRM_TIMEOUT", "2.4"))
SEND_CONFIRM_TIMEOUT = float(os.getenv("ZALO_SEND_CONFIRM_TIMEOUT", "1.8"))
FAILURE_NOTIFY_TIMEOUT = float(os.getenv("ZALO_FAILURE_NOTIFY_TIMEOUT", "8.0"))
SEARCH_RETRY_COUNT = int(os.getenv("ZALO_SEARCH_RETRY_COUNT", "1"))
COMPOSER_VERIFY_TIMEOUT = float(os.getenv("ZALO_COMPOSER_VERIFY_TIMEOUT", "0.9"))
MIN_SEND_INTERVAL_SECONDS = float(os.getenv("ZALO_MIN_SEND_INTERVAL_SECONDS", "0.8"))
NOTICE_SEND_RETRY_COUNT = int(os.getenv("ZALO_NOTICE_SEND_RETRY_COUNT", "2"))
NOTICE_SEND_TIMEOUT = float(os.getenv("ZALO_NOTICE_SEND_TIMEOUT", "6.0"))

driver = None
_recent_sends = {}
_last_send_trigger_ts = 0.0


# -----------------------------------------------------------------------------
# BOOT
# -----------------------------------------------------------------------------
def initialize_driver():
    global driver
    print("\n=======================================================")
    print("[ZALO] ĐANG KHỞI ĐỘNG ZALO GATEWAY SERVER...")
    print("=======================================================")

    try:
        print("[1/3] Thiết lập Chrome profile...")
        chrome_options = Options()
        current_dir = os.path.dirname(os.path.abspath(__file__))
        profile_path = os.path.join(current_dir, "ZaloBotProfile")

        chrome_options.add_argument(f"--user-data-dir={profile_path}")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-notifications")
        chrome_options.add_argument("--disable-popup-blocking")

        print("[2/3] Kết nối trình duyệt...")
        try:
            driver = webdriver.Chrome(options=chrome_options)
        except Exception:
            driver = webdriver.Chrome(
                service=Service(ChromeDriverManager().install()),
                options=chrome_options,
            )

        print("[3/3] Mở Zalo Web...")
        driver.get("https://chat.zalo.me/")

        print("[OK] Zalo Gateway đang lắng nghe tại cổng 5000.")
        print("[OK] Số nhận cảnh báo lỗi:", ZALO_FAILURE_NOTIFY_PHONE)
        print("Nếu chưa đăng nhập, hãy quét QR trên cửa sổ Chrome vừa mở.")
        print("=======================================================\n")
    except Exception:
        print("\n[LỖI] Không khởi động được trình duyệt:")
        print(traceback.format_exc())
        print("=======================================================\n")



# -----------------------------------------------------------------------------
# GENERAL HELPERS
# -----------------------------------------------------------------------------
def normalize_vietnam_phone(value):
    if value is None:
        return ""

    phone = str(value).strip()
    if phone.endswith(".0"):
        phone = phone[:-2]

    phone = re.sub(r"\D", "", phone)

    if phone.startswith("84") and len(phone) == 11:
        phone = "0" + phone[2:]
    elif len(phone) == 9:
        phone = "0" + phone

    if not re.fullmatch(r"0[35789]\d{8}", phone):
        return ""
    return phone


def digits_only(value):
    return re.sub(r"\D", "", str(value or ""))


def norm_text(value):
    text = unicodedata.normalize("NFC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()


def first_meaningful_line(text):
    for line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            return line
    return ""


def build_dedupe_key(phone, message, request_id=None):
    if request_id:
        return "rid:" + str(request_id).strip()

    raw = f"{phone}\n{message.strip()}".encode("utf-8")
    return "msg:" + hashlib.sha256(raw).hexdigest()


def cleanup_recent_sends():
    now = time.time()
    for key, ts in list(_recent_sends.items()):
        if now - ts > DEDUPE_SECONDS:
            _recent_sends.pop(key, None)


def save_debug_screenshot(tag):
    if driver is None:
        return None
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe_tag = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(tag))[:70]
        path = os.path.join(
            DEBUG_DIR,
            f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{safe_tag}.png",
        )
        driver.save_screenshot(path)
        return path
    except Exception:
        return None


def is_displayed(element):
    try:
        return bool(element and element.is_displayed())
    except Exception:
        return False


def is_enabled(element):
    try:
        return bool(element and element.is_enabled())
    except Exception:
        return False


def element_text(element):
    try:
        return (element.text or "").strip()
    except Exception:
        return ""


def safe_click(element):
    try:
        element.click()
        return True
    except (ElementClickInterceptedException, WebDriverException, Exception):
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', inline:'nearest'});"
                "arguments[0].click();",
                element,
            )
            return True
        except Exception:
            return False


# -----------------------------------------------------------------------------
# CHAT / HEADER
# -----------------------------------------------------------------------------
def find_visible_chat_input(timeout=3.0):
    selectors = [
        (By.ID, "richInput"),
        (By.ID, "chatInput"),
        (By.CSS_SELECTOR, "[data-testid='chat-input']"),
        (By.CSS_SELECTOR, "div[contenteditable='true'][role='textbox']"),
        (By.CSS_SELECTOR, "div[contenteditable='true']"),
    ]

    deadline = time.time() + timeout
    while time.time() < deadline:
        for by, sel in selectors:
            try:
                for el in driver.find_elements(by, sel):
                    if not is_displayed(el) or not is_enabled(el):
                        continue
                    rect = el.rect
                    if rect.get("width", 0) >= 180 and rect.get("height", 0) >= 18:
                        return el
            except Exception:
                continue
        time.sleep(0.08)
    return None


def get_chat_input_recipient_hint(chat_box):
    hints = []
    for attr in ("placeholder", "data-placeholder", "aria-label", "title"):
        try:
            value = chat_box.get_attribute(attr)
            if value:
                hints.append(value)
        except Exception:
            pass

    # Lấy text gần composer, vì Zalo có thể render placeholder bằng pseudo-element.
    try:
        nearby = driver.execute_script(
            """
            const el = arguments[0];
            const roots = [
                el,
                el.parentElement,
                el.parentElement?.parentElement,
                el.closest('[contenteditable="true"]')?.parentElement,
                el.closest('.chat-input__content')
            ].filter(Boolean);

            const out = [];
            for (const r of roots) {
                const t = (r.innerText || r.textContent || '').trim();
                if (t) out.push(t);
            }
            return [...new Set(out)].join(' | ');
            """,
            chat_box,
        )
        if nearby:
            hints.append(nearby)
    except Exception:
        pass

    return " | ".join(x for x in hints if x)


def get_active_chat_header_hint():
    # Selector cụ thể nếu có.
    selectors = (
        ".title.header-title",
        ".chat-info__header__title",
        "[class*='header-title']",
        "[class*='chat-info'] [class*='title']",
    )
    for sel in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if not is_displayed(el):
                    continue
                txt = first_meaningful_line(element_text(el))
                if 2 <= len(txt) <= 120:
                    return txt
        except Exception:
            pass

    # Fallback: quét vùng header phía trên bên phải.
    try:
        value = driver.execute_script(
            """
            const W = window.innerWidth;
            const H = window.innerHeight;
            const leftMin = Math.max(390, W * 0.28);
            const out = [];

            for (const el of document.querySelectorAll('div,span,p,h1,h2,h3')) {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                if (!r.width || !r.height) continue;
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                if (r.left < leftMin || r.top < 155 || r.top > 255) continue;
                if (r.width > W * .7 || r.height > 70) continue;

                const t = (el.innerText || '').trim().replace(/\\s+/g, ' ');
                if (!t || t.length < 2 || t.length > 120) continue;
                if (/Chrome is being controlled|Tin nhắn|Sticker|Hình ảnh/i.test(t)) continue;

                const fs = parseFloat(st.fontSize || '0');
                const fw = parseInt(st.fontWeight || '400', 10) || 400;
                let score = fs;
                if (fw >= 500) score += 8;
                if (r.top < 220) score += 6;
                if (r.left < W * .75) score += 4;

                out.push({t, score, y:r.top, x:r.left});
            }

            out.sort((a,b) => (b.score-a.score) || (a.y-b.y) || (a.x-b.x));
            return out.length ? out[0].t : '';
            """,
        )
        return first_meaningful_line(value or "")
    except Exception:
        return ""


# -----------------------------------------------------------------------------
# SEARCH OVERLAY / UI STATE
# -----------------------------------------------------------------------------
def close_search_overlay():
    """Đóng panel tìm kiếm nếu đang mở, đặc biệt hữu ích sau khi chọn kết quả."""
    selectors = (
        "button[aria-label='Đóng']",
        "button[title='Đóng']",
        "[aria-label='Đóng']",
        "[title='Đóng']",
    )
    for sel in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if is_displayed(el) and is_enabled(el) and safe_click(el):
                    time.sleep(0.05)
                    return True
        except Exception:
            pass

    # Fallback theo text - chỉ trong panel trái.
    try:
        clicked = driver.execute_script(
            """
            const W = window.innerWidth;
            for (const el of document.querySelectorAll('button,div,span')) {
                const r = el.getBoundingClientRect();
                if (!r.width || !r.height || r.left > W * .45 || r.top < 100 || r.top > 280) continue;
                const t = (el.innerText || el.textContent || '').trim();
                if (t === 'Đóng' || t === 'Close') { el.click(); return true; }
            }
            return false;
            """
        )
        return bool(clicked)
    except Exception:
        return False


def is_search_box_value(search_box, phone):
    try:
        return normalize_vietnam_phone(search_box.get_attribute('value') or '') == normalize_vietnam_phone(phone)
    except Exception:
        return False


# -----------------------------------------------------------------------------
# SEARCH BOX + SEARCH RESULT
# -----------------------------------------------------------------------------
def find_zalo_search_box(prefer_contact=False, timeout=2.5):
    """Tìm ô global trước; contact khi prefer_contact=True."""
    contact_selectors = (
        (By.ID, "contact-search-input"),
        (By.CSS_SELECTOR, "input[name='contact-search']"),
        (By.CSS_SELECTOR, "input[placeholder*='Danh bạ']"),
        (By.CSS_SELECTOR, "input[placeholder*='tìm trong danh bạ']"),
    )

    global_selectors = (
        (By.CSS_SELECTOR, "input[placeholder='Tìm kiếm']"),
        (By.CSS_SELECTOR, "input[placeholder*='Tìm kiếm']"),
        (By.CSS_SELECTOR, "input[aria-label*='Tìm kiếm']"),
        (By.CSS_SELECTOR, "input[name*='search']"),
        (By.CSS_SELECTOR, "input[id*='search']"),
    )

    selectors = (
        contact_selectors + global_selectors
        if prefer_contact
        else global_selectors + contact_selectors
    )

    deadline = time.time() + timeout
    while time.time() < deadline:
        for by, sel in selectors:
            try:
                for el in driver.find_elements(by, sel):
                    if not is_displayed(el) or not is_enabled(el):
                        continue

                    el_id = (el.get_attribute("id") or "").strip().lower()
                    el_name = (el.get_attribute("name") or "").strip().lower()

                    if not prefer_contact and (
                        el_id == "contact-search-input" or el_name == "contact-search"
                    ):
                        continue

                    rect = el.rect
                    if rect.get("width", 0) >= 140 and rect.get("height", 0) >= 24:
                        return el
            except Exception:
                continue
        time.sleep(0.06)

    return None


def clear_search_box(search_box):
    try:
        safe_click(search_box)
    except Exception:
        pass

    try:
        search_box.send_keys(Keys.CONTROL + "a")
        search_box.send_keys(Keys.BACK_SPACE)
    except Exception:
        try:
            driver.execute_script(
                "arguments[0].focus(); arguments[0].select();", search_box
            )
            search_box.send_keys(Keys.BACK_SPACE)
        except Exception:
            pass


def prepare_search_box(search_box, query):
    clear_search_box(search_box)
    try:
        search_box.send_keys(query)
    except Exception:
        return ""

    try:
        return (search_box.get_attribute("value") or "").strip()
    except Exception:
        return ""


def submit_search_query(search_box):
    try:
        safe_click(search_box)
        search_box.send_keys(Keys.ENTER)
        return True
    except Exception:
        return False


def _scope_elements_for_search():
    """Tìm các container gần ô search có tính chất search/result."""
    selectors = (
        "#searchResultList",
        "[id*='searchResult']",
        ".search-result",
        "[class*='search-result']",
        "[role='listbox']",
    )
    out = []
    seen = set()

    for sel in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if not is_displayed(el):
                    continue
                if id(el) in seen:
                    continue
                seen.add(id(el))
                out.append(el)
        except Exception:
            pass
    return out


def _element_digits(el):
    try:
        return driver.execute_script(
            """
            const el = arguments[0];
            return [
                el.innerText || '', el.textContent || '',
                el.getAttribute('data-phone') || '',
                el.getAttribute('phone') || '',
                el.getAttribute('data-id') || '',
                el.getAttribute('data-uid') || '',
                el.getAttribute('data-user-id') || '',
                el.getAttribute('href') || ''
            ].join(' ').replace(/\\D/g,'');
            """,
            el,
        ) or ""
    except Exception:
        return ""


def _candidate_row_selectors():
    return (
        ".list-friend-conctact",
        ".suggest-friend-item",
        ".contact-list-item",
        ".search-result-item",
        ".conv-item",
        "[role='option']",
        "[role='listitem']",
        "li",
        "a",
        "button",
    )


def find_visible_phone_row(target_phone):
    """Tìm row đang render có CHÍNH số điện thoại mục tiêu.

    Đây là đường ưu tiên nhất cho trường hợp bạn đã có sẵn:
    ví dụ row: 'Thd.ThayDuc' + subtitle '0906576928'.
    """
    target = digits_only(normalize_vietnam_phone(target_phone))
    tail = target[-9:] if len(target) >= 9 else target
    if not target:
        return None

    # Không dùng search box; chỉ panel trái.
    try:
        candidate = driver.execute_script(
            """
            const target = arguments[0], tail = arguments[1];
            const roots = [
                '#conversationList','#chatList','#contactList',
                '.chat-list','.conversation-list','.contact-list','.friend-list'
            ];
            const rowSelectors = [
                '.conv-item','.contact-list-item','.list-friend-conctact',
                '.suggest-friend-item','.search-result-item',
                '[role="listitem"]','[role="option"]','li','a','button'
            ];
            const visible = (el) => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return !!(r.width && r.height && r.right > 0 && r.bottom > 0 &&
                    st.display !== 'none' && st.visibility !== 'hidden' &&
                    st.opacity !== '0');
            };
            const digitsOf = (el) => [
                el.innerText || '', el.textContent || '',
                el.getAttribute('data-phone') || '',
                el.getAttribute('phone') || '',
                el.getAttribute('data-id') || '',
                el.getAttribute('data-uid') || '',
                el.getAttribute('data-user-id') || '',
                el.getAttribute('href') || ''
            ].join(' ').replace(/\\D/g,'');

            const validRow = (el) => {
                if (!visible(el)) return false;
                const r = el.getBoundingClientRect();
                if (r.left > Math.max(520, window.innerWidth * .48)) return false;
                if (r.width < 140 || r.height < 30 || r.height > 150) return false;
                const t = (el.innerText || '').trim().replace(/\\s+/g,' ');
                return t.length >= 3 && t.length <= 240;
            };

            const found = [];
            const seen = new Set();

            const scan = (root) => {
                for (const sel of rowSelectors) {
                    for (const el of root.querySelectorAll(sel)) {
                        if (seen.has(el) || !validRow(el)) continue;
                        seen.add(el);

                        const ds = digitsOf(el);
                        if (!((target && ds.includes(target)) || (tail && ds.includes(tail)))) continue;

                        // Nếu phần tử match là span/text con, chỉ leo lên tới
                        // ancestor gần nhất có hình dạng một row. Không leo nhiều tầng
                        // vì ancestor lớn có thể là cả danh sách và sẽ click sai.
                        let best = validRow(el) ? el : null;
                        let p = el.parentElement;
                        for (let i=0; i<2 && !best && p; i++, p=p.parentElement) {
                            if (!validRow(p)) continue;
                            const pds = digitsOf(p);
                            if ((target && pds.includes(target)) || (tail && pds.includes(tail))) {
                                best = p;
                            }
                        }
                        if (!best) continue;

                        const r = best.getBoundingClientRect();
                        let score = ds.includes(target) ? 300 : 220;
                        const cls = String(best.className || '').toLowerCase();
                        if (/conv|contact|friend|chat|conversation|list-item/.test(cls)) score += 40;
                        if (r.height >= 42 && r.height <= 100) score += 10;
                        found.push({el:best, score, y:r.top});
                    }
                }
            };

            for (const sel of roots) {
                for (const root of document.querySelectorAll(sel)) {
                    if (visible(root)) scan(root);
                }
            }

            found.sort((a,b) => (b.score-a.score) || (a.y-b.y));
            return found.length ? found[0].el : null;
            """,
            target,
            tail,
        )
        if candidate is not None and is_displayed(candidate):
            ds = _element_digits(candidate)
            if target in ds or (tail and tail in ds):
                return candidate
    except Exception:
        pass

    return None


def _find_search_result_row(search_box, target_phone):
    """Chỉ chấp nhận row trong search-result container hoặc row chứa đúng SĐT."""
    target = digits_only(normalize_vietnam_phone(target_phone))
    tail = target[-9:] if len(target) >= 9 else target

    try:
        candidate = driver.execute_script(
            """
            const sb = arguments[0];
            const target = arguments[1], tail = arguments[2];
            const sr = sb ? sb.getBoundingClientRect() : {bottom:0};
            const W = window.innerWidth;

            const containers = [
                '#searchResultList',
                '[id*="searchResult"]',
                '.search-result',
                '[class*="search-result"]',
                '[role="listbox"]'
            ];
            const rowSelectors = [
                '.list-friend-conctact','.suggest-friend-item',
                '.contact-list-item','.search-result-item',
                '.conv-item','[role="option"]','[role="listitem"]'
            ];

            const visible = (el) => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return !!(r.width && r.height && r.right > 0 && r.bottom > 0 &&
                    st.display !== 'none' && st.visibility !== 'hidden' &&
                    st.opacity !== '0');
            };

            const digitsOf = (el) => [
                el.innerText || '', el.textContent || '',
                el.getAttribute('data-phone') || '',
                el.getAttribute('phone') || '',
                el.getAttribute('data-id') || '',
                el.getAttribute('data-uid') || '',
                el.getAttribute('data-user-id') || '',
                el.getAttribute('href') || ''
            ].join(' ').replace(/\\D/g,'');

            const rows = [];
            const seen = new Set();

            const add = (el, strong) => {
                if (!visible(el) || seen.has(el)) return;
                const r = el.getBoundingClientRect();
                if (r.left > Math.max(540, W * .55)) return;
                if (r.top < sr.bottom - 8) return;
                if (r.width < 140 || r.height < 28 || r.height > 170) return;

                const text = (el.innerText || '').trim().replace(/\\s+/g,' ');
                if (!text || text.length > 260) return;

                const ds = digitsOf(el);
                const exactPhone = !!(target && ds.includes(target));
                const phoneTail = !!(tail && ds.includes(tail));

                // Với row ngoài search container, bắt buộc chứa số đích.
                if (!strong && !exactPhone && !phoneTail) return;

                let score = strong ? 100 : 20;
                if (exactPhone) score += 220;
                else if (phoneTail) score += 150;

                const cls = String(el.className || '').toLowerCase();
                if (cls.includes('list-friend-conctact')) score += 40;
                if (cls.includes('suggest-friend-item')) score += 35;
                if (cls.includes('contact-list-item')) score += 30;
                if (cls.includes('search-result')) score += 35;

                rows.push({el,score,y:r.top,text});
                seen.add(el);
            };

            // Search result thật.
            for (const cs of containers) {
                for (const c of document.querySelectorAll(cs)) {
                    if (!visible(c)) continue;
                    const cr = c.getBoundingClientRect();
                    if (cr.left > Math.max(540, W * .55)) continue;

                    for (const rs of rowSelectors) {
                        for (const row of c.querySelectorAll(rs)) {
                            add(row, true);
                        }
                    }
                }
            }

            // Fallback: chỉ row chứa đúng số.
            if (!rows.length) {
                for (const rs of rowSelectors) {
                    for (const row of document.querySelectorAll(rs)) {
                        add(row, false);
                    }
                }
            }

            rows.sort((a,b) => (b.score-a.score) || (a.y-b.y));
            return rows.length ? rows[0].el : null;
            """,
            search_box,
            target,
            tail,
        )
        if candidate is not None and is_displayed(candidate):
            return candidate
    except Exception:
        pass

    return None


def _find_phone_friend_result(search_box, target_phone):
    """Tìm CHÍNH XÁC card "Tìm bạn qua số điện thoại" của UI Zalo mới.

    Trường hợp thực tế thường có dạng:
        Tìm bạn qua số điện thoại:
        <Tên Zalo>
        Số điện thoại: 0xxxxxxxxx
        Tin nhắn (n)
        <chat cũ chứa lại số điện thoại>

    Nếu chỉ quét chuỗi số, code rất dễ chọn nhầm dòng "Tin nhắn" bên dưới.
    Hàm này ưu tiên card có nhãn "Số điện thoại:" và chọn phần tử nhỏ nhất
    chứa đúng SĐT, đồng thời loại bỏ row lịch sử chat.
    """
    target = digits_only(normalize_vietnam_phone(target_phone))
    tail = target[-9:] if len(target) >= 9 else target
    if not target:
        return None

    try:
        candidate = driver.execute_script(
            """
            const sb = arguments[0];
            const target = arguments[1] || '';
            const tail = arguments[2] || '';
            const W = window.innerWidth, H = window.innerHeight;
            const sr = sb ? sb.getBoundingClientRect() : {bottom: 100};

            const visible = (el) => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return !!(r.width && r.height && r.right > 0 && r.bottom > 0 && r.top < H &&
                    st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0');
            };
            const digits = (v) => String(v || '').replace(/\\D/g, '');
            const rows = [];
            const seen = new Set();

            // Scan cả div vì Zalo thường không gắn role/class ổn định cho card này.
            const selectors = [
                '.list-friend-conctact','.suggest-friend-item','.contact-list-item',
                '.search-result-item','[role="option"]','[role="listitem"]',
                'li','a','button','div'
            ];

            const consider = (el) => {
                if (!visible(el) || seen.has(el)) return;
                const r = el.getBoundingClientRect();
                if (r.left > Math.max(520, W * .48)) return;
                if (r.top < sr.bottom - 6) return;
                if (r.width < 150 || r.width > Math.max(520, W * .50)) return;
                if (r.height < 36 || r.height > 145) return;

                const raw = (el.innerText || el.textContent || '').trim();
                const text = raw.replace(/\\s+/g, ' ').trim();
                if (!text || text.length > 320) return;

                const allDigits = digits([
                    raw,
                    el.getAttribute('data-phone') || '',
                    el.getAttribute('phone') || '',
                    el.getAttribute('href') || ''
                ].join(' '));
                const exactPhone = allDigits.includes(target);
                const phoneTail = !!tail && allDigits.includes(tail);
                if (!exactPhone && !phoneTail) return;

                const hasPhoneLabel = /Số điện thoại\\s*:/i.test(text);
                const isMessageHistory = /(^|\\s)Bạn\\s*:|Tin nhắn\\s*\\(/i.test(text);
                const isFriendHeaderOnly = /^Tìm bạn qua số điện thoại\\s*:?$/i.test(text);
                if (isFriendHeaderOnly) return;

                let score = exactPhone ? 700 : 560;
                if (hasPhoneLabel) score += 420;
                if (/Tìm bạn qua số điện thoại/i.test(text)) score += 40;
                if (isMessageHistory) score -= 520;

                const cls = String(el.className || '').toLowerCase();
                if (/friend|contact|search-result|suggest|list-item/.test(cls)) score += 80;
                if (/conv-item|conversation/.test(cls)) score -= 180;

                // Ưu tiên card vừa đủ nhỏ thay vì container lớn chứa cả kết quả + lịch sử chat.
                score -= Math.max(0, r.height - 82) * 2;
                if (r.height >= 46 && r.height <= 110) score += 40;

                seen.add(el);
                rows.push({el, score, y:r.top, h:r.height, text});
            };

            for (const sel of selectors) {
                for (const el of document.querySelectorAll(sel)) consider(el);
            }

            rows.sort((a,b) => (b.score-a.score) || (a.h-b.h) || (a.y-b.y));
            return rows.length ? rows[0].el : null;
            """,
            search_box,
            target,
            tail,
        )
        if candidate is not None and is_displayed(candidate):
            txt = _candidate_debug_text(candidate)
            if target in digits_only(txt) or (tail and tail in digits_only(txt)):
                return candidate
    except Exception:
        pass
    return None


def _find_left_search_candidate(search_box, target_phone="", target_name=""):
    """Fallback bền vững với UI Zalo mới.

    Không phụ thuộc class CSS cố định. Chỉ quét panel trái, ưu tiên:
    1) dòng có Tên Zalo khớp chính xác;
    2) dòng có đúng SĐT;
    và hạ điểm các dòng thuộc mục "Tin nhắn" để tránh bấm nhầm lịch sử chat.
    """
    target = digits_only(normalize_vietnam_phone(target_phone)) if target_phone else ""
    tail = target[-9:] if len(target) >= 9 else target
    expected = norm_text(target_name)

    try:
        candidate = driver.execute_script(
            """
            const sb = arguments[0];
            const target = arguments[1] || '';
            const tail = arguments[2] || '';
            const expected = arguments[3] || '';
            const W = window.innerWidth, H = window.innerHeight;
            const sr = sb ? sb.getBoundingClientRect() : {bottom: 120};

            const norm = (v) => String(v || '')
                .normalize('NFC')
                .toLocaleLowerCase('vi-VN')
                .replace(/\\s+/g, ' ')
                .trim();
            const digits = (v) => String(v || '').replace(/\\D/g, '');
            const visible = (el) => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return !!(r.width && r.height && r.right > 0 && r.bottom > 0 &&
                    r.top < H && st.display !== 'none' && st.visibility !== 'hidden' &&
                    st.opacity !== '0');
            };

            const rows = [];
            const seen = new Set();
            const selectors = [
                '.list-friend-conctact','.suggest-friend-item','.contact-list-item',
                '.search-result-item','.conv-item','[role="option"]','[role="listitem"]',
                'li','a','button','div'
            ];

            const consider = (el) => {
                if (!visible(el) || seen.has(el)) return;
                const r = el.getBoundingClientRect();
                if (r.left > Math.max(520, W * .48)) return;
                if (r.top < sr.bottom - 10) return;
                if (r.width < 150 || r.width > Math.max(520, W * .5)) return;
                if (r.height < 38 || r.height > 135) return;

                const raw = (el.innerText || el.textContent || '').trim();
                const text = raw.replace(/\\s+/g, ' ').trim();
                if (!text || text.length > 320) return;
                if (/^(Tất cả|Liên hệ|Tin nhắn|File|Đóng)$/i.test(text)) return;
                if (/^Tìm bạn qua số điện thoại\\s*:?$/i.test(text)) return;

                const allDigits = digits([
                    raw,
                    el.getAttribute('data-phone') || '',
                    el.getAttribute('phone') || '',
                    el.getAttribute('href') || ''
                ].join(' '));

                const lines = raw.split(/\n+/).map(norm).filter(Boolean);
                const textNorm = norm(text);
                const exactName = !!expected && lines.some(x => x === expected);
                const startsName = !!expected && (textNorm === expected || textNorm.startsWith(expected + ' '));
                const exactPhone = !!target && allDigits.includes(target);
                const phoneTail = !!tail && allDigits.includes(tail);

                if (!exactName && !startsName && !exactPhone && !phoneTail) return;

                let score = 0;
                if (exactName) score += 500;
                else if (startsName) score += 350;
                if (exactPhone) score += 320;
                else if (phoneTail) score += 230;

                const cls = String(el.className || '').toLowerCase();
                if (/friend|contact|search-result|suggest|conv-item|list-item/.test(cls)) score += 55;
                if (/Số điện thoại\\s*:/i.test(text)) score += 70;
                if (/Bạn\\s*:|Tin nhắn\\s*\\(/i.test(text)) score -= 520;
                if (r.height >= 48 && r.height <= 105) score += 20;

                seen.add(el);
                rows.push({el, score, y:r.top, h:r.height, text});
            };

            for (const sel of selectors) {
                for (const el of document.querySelectorAll(sel)) consider(el);
            }

            rows.sort((a,b) => (b.score-a.score) || (a.h-b.h) || (a.y-b.y));
            return rows.length ? rows[0].el : null;
            """,
            search_box,
            target,
            tail,
            expected,
        )
        if candidate is not None and is_displayed(candidate):
            return candidate
    except Exception:
        pass
    return None


def _candidate_debug_text(candidate):
    try:
        return re.sub(r"\s+", " ", element_text(candidate)).strip()[:240]
    except Exception:
        return ""


def search_once(search_box, query, target_phone, timeout=2.5):
    typed = prepare_search_box(search_box, query)
    if digits_only(typed) and normalize_vietnam_phone(typed) != normalize_vietnam_phone(target_phone):
        return None

    # 1) Search khi vừa gõ. UI Zalo mới thường render kết quả ngay.
    deadline = time.time() + min(0.55, timeout)
    while time.time() < deadline:
        candidate = _find_phone_friend_result(search_box, target_phone)
        if candidate is None:
            candidate = _find_left_search_candidate(search_box, target_phone=target_phone)
        if candidate is None:
            candidate = _find_search_result_row(search_box, target_phone)
        if candidate is not None:
            print(f"[ZALO][SEARCH_PHONE] thấy row: {_candidate_debug_text(candidate)!r}")
            return candidate

        # Trường hợp đã là bạn và số đã hiển thị trên panel.
        direct = find_visible_phone_row(target_phone)
        if direct is not None:
            return direct

        time.sleep(0.05)

    # 2) Một số bản Zalo chỉ submit full search bằng ENTER.
    submit_search_query(search_box)

    deadline = time.time() + timeout
    while time.time() < deadline:
        candidate = _find_phone_friend_result(search_box, target_phone)
        if candidate is None:
            candidate = _find_left_search_candidate(search_box, target_phone=target_phone)
        if candidate is None:
            candidate = _find_search_result_row(search_box, target_phone)
        if candidate is not None:
            print(f"[ZALO][SEARCH_PHONE] thấy row sau ENTER: {_candidate_debug_text(candidate)!r}")
            return candidate

        direct = find_visible_phone_row(target_phone)
        if direct is not None:
            return direct

        time.sleep(0.05)

    return None


def _visible_name_search_rows(search_box):
    """Thu thập các row kết quả đang hiển thị bên dưới ô tìm kiếm, chỉ ở panel trái."""
    rows = []
    seen = set()
    selectors = _candidate_row_selectors()

    for scope in _scope_elements_for_search():
        for sel in selectors:
            try:
                for el in scope.find_elements(By.CSS_SELECTOR, sel):
                    if not is_displayed(el):
                        continue
                    key = getattr(el, "id", None) or str(id(el))
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(el)
            except Exception:
                continue

    if not rows:
        try:
            sr = search_box.rect
            bottom = float(sr.get("y", 0)) + float(sr.get("height", 0)) - 8
            win_width = driver.get_window_size().get("width", 1200)
        except Exception:
            bottom = 0
            win_width = 1200

        for sel in selectors:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    if not is_displayed(el):
                        continue
                    try:
                        r = el.rect
                        if r.get("x", 99999) > max(540, win_width * 0.55):
                            continue
                        if r.get("y", 0) < bottom:
                            continue
                        if r.get("width", 0) < 140 or not (28 <= r.get("height", 0) <= 170):
                            continue
                    except Exception:
                        continue
                    key = getattr(el, "id", None) or str(id(el))
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(el)
            except Exception:
                continue
    return rows


def _find_exact_name_result_smart(search_box, target_name):
    """Quét UI động và trả (candidate, count) cho tên Zalo khớp chính xác.

    Dùng vị trí/khung row thay vì phụ thuộc class CSS để chịu được Zalo đổi giao diện.
    Các phần tử lồng nhau của cùng một row được gom theo vị trí/text để không bị tính trùng.
    """
    expected = norm_text(target_name)
    if len(expected) < 2:
        return None, 0
    try:
        result = driver.execute_script(
            """
            const sb = arguments[0];
            const expected = arguments[1] || '';
            const W = window.innerWidth, H = window.innerHeight;
            const sr = sb ? sb.getBoundingClientRect() : {bottom: 120};
            const norm = (v) => String(v || '')
                .normalize('NFC')
                .toLocaleLowerCase('vi-VN')
                .replace(/\\s+/g,' ')
                .trim();
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return !!(r.width && r.height && r.right > 0 && r.bottom > 0 && r.top < H &&
                    st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0');
            };
            const found = [];
            const selectors = [
                '.list-friend-conctact','.suggest-friend-item','.contact-list-item',
                '.search-result-item','.conv-item','[role="option"]','[role="listitem"]',
                'li','a','button','div'
            ];
            for (const sel of selectors) {
                for (const el of document.querySelectorAll(sel)) {
                    if (!visible(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.left > Math.max(520, W * .48)) continue;
                    if (r.top < sr.bottom - 10) continue;
                    if (r.width < 150 || r.width > Math.max(520, W * .5)) continue;
                    if (r.height < 38 || r.height > 135) continue;
                    const raw = (el.innerText || el.textContent || '').trim();
                    if (!raw || raw.length > 320) continue;
                    const text = raw.replace(/\\s+/g,' ').trim();
                    if (/^Tìm bạn qua số điện thoại\\s*:?$/i.test(text)) continue;
                    const lines = raw.split(/\n+/).map(norm).filter(Boolean);
                    const textNorm = norm(text);
                    const exact = lines.some(x => x === expected) || textNorm === expected || textNorm.startsWith(expected + ' ');
                    if (!exact) continue;
                    let score = 400;
                    const cls = String(el.className || '').toLowerCase();
                    if (/friend|contact|search-result|suggest|conv-item|list-item/.test(cls)) score += 55;
                    if (/Bạn\\s*:|Tin nhắn\\s*\\(/i.test(text)) score -= 180;
                    if (r.height >= 48 && r.height <= 105) score += 20;
                    found.push({el, score, y:r.top, h:r.height, text:textNorm});
                }
            }
            found.sort((a,b) => (b.score-a.score) || (a.h-b.h) || (a.y-b.y));
            const unique = [];
            for (const item of found) {
                const same = unique.some(x => Math.abs(x.y-item.y) <= 4 && (x.text === item.text || x.text.includes(item.text) || item.text.includes(x.text)));
                if (!same) unique.push(item);
            }
            return {count: unique.length, element: unique.length === 1 ? unique[0].el : null};
            """,
            search_box,
            expected,
        ) or {}
        count = int(result.get("count") or 0)
        candidate = result.get("element")
        if candidate is not None and is_displayed(candidate):
            return candidate, count
        return None, count
    except Exception:
        return None, 0


def find_exact_name_result(search_box, target_name):
    """Trả (candidate, status); không chọn khi có nhiều tài khoản cùng tên."""
    expected = norm_text(target_name)
    if len(expected) < 2:
        return None, "INVALID_ZALO_NAME"

    # Quét UI mới trước. Nếu có >1 row độc lập cùng tên, hủy để tránh gửi nhầm.
    smart_candidate, smart_count = _find_exact_name_result_smart(search_box, target_name)
    if smart_count > 1:
        return None, "AMBIGUOUS_ZALO_NAME"
    if smart_candidate is not None:
        extracted = extract_candidate_name(smart_candidate)
        if norm_text(extracted) == expected:
            print(f"[ZALO][SEARCH_NAME] thấy row: {_candidate_debug_text(smart_candidate)!r}")
            return smart_candidate, ""

    matches = []
    seen = set()
    for row in _visible_name_search_rows(search_box):
        name = extract_candidate_name(row)
        if norm_text(name) != expected:
            continue
        key = getattr(row, "id", None) or str(id(row))
        if key in seen:
            continue
        seen.add(key)
        matches.append((row, name))

    if len(matches) == 1:
        return matches[0][0], ""
    if len(matches) > 1:
        return None, "AMBIGUOUS_ZALO_NAME"
    return None, "NOT_FOUND"


def search_name_once(search_box, zalo_name, timeout=2.5):
    prepare_search_box(search_box, zalo_name)

    deadline = time.time() + min(0.8, timeout)
    while time.time() < deadline:
        found, status = find_exact_name_result(search_box, zalo_name)
        if found is not None or status == "AMBIGUOUS_ZALO_NAME":
            return found, status
        time.sleep(0.06)

    submit_search_query(search_box)
    deadline = time.time() + timeout
    while time.time() < deadline:
        found, status = find_exact_name_result(search_box, zalo_name)
        if found is not None or status == "AMBIGUOUS_ZALO_NAME":
            return found, status
        time.sleep(0.06)

    return None, "NOT_FOUND"


# -----------------------------------------------------------------------------
# CONTACT / RECIPIENT
# -----------------------------------------------------------------------------
def extract_candidate_name(candidate, target_phone=""):
    """Lấy tên Zalo từ một row/card kết quả mà không ăn nhầm tiêu đề/nhãn.

    target_phone giúp loại bỏ dòng "Số điện thoại: ...". Đây là điểm quan trọng
    với giao diện Global Search mới, nơi card kết quả có thể nằm chung gần mục
    "Tìm bạn qua số điện thoại" và "Tin nhắn".
    """
    target = digits_only(normalize_vietnam_phone(target_phone)) if target_phone else ""
    tail = target[-9:] if len(target) >= 9 else target

    selectors = (
        ".list-friend-conctact__title",
        ".sgf-name",
        ".item-title-name",
        ".item-title",
        "[data-id*='name']",
        "[class*='name']",
    )
    ignored_exact = {
        "tất cả", "liên hệ", "tin nhắn", "file", "đóng",
        "tìm bạn qua số điện thoại", "tìm bạn qua số điện thoại:",
        "số điện thoại", "số điện thoại:",
    }

    def valid_name_text(txt):
        txt = re.sub(r"\s+", " ", str(txt or "")).strip()
        if not (2 <= len(txt) <= 120):
            return ""
        low = norm_text(txt)
        if low in ignored_exact:
            return ""
        if re.fullmatch(r"[+()\-\s0-9.]+", txt):
            return ""
        if re.match(r"(?i)^số điện thoại\s*:", txt):
            return ""
        if re.match(r"(?i)^tin nhắn\s*\(", txt):
            return ""
        if re.match(r"(?i)^bạn\s*:", txt):
            return ""
        ds = digits_only(txt)
        if target and (target in ds or (tail and tail in ds)):
            return ""
        # Tên phải có ít nhất một chữ cái để không nhận nhãn/ký hiệu.
        if not re.search(r"[^\W\d_]", txt, re.UNICODE):
            return ""
        return txt

    for sel in selectors:
        try:
            for el in candidate.find_elements(By.CSS_SELECTOR, sel):
                txt = valid_name_text(first_meaningful_line(element_text(el)))
                if txt:
                    return txt
        except Exception:
            pass

    text = element_text(candidate)
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    for line in lines:
        txt = valid_name_text(line)
        if txt:
            return txt

    # Trường hợp DOM nén toàn bộ card thành một dòng: cắt các nhãn quen thuộc.
    compact = re.sub(r"\s+", " ", text).strip()
    compact = re.sub(r"(?i)^tìm bạn qua số điện thoại\s*:?\s*", "", compact)
    compact = re.split(r"(?i)\s+số điện thoại\s*:", compact, maxsplit=1)[0].strip()
    txt = valid_name_text(compact)
    return txt


def get_active_chat_state(chat_box=None):
    hints = [
        get_active_chat_header_hint(),
        get_chat_input_recipient_hint(chat_box) if chat_box is not None else "",
    ]

    # title trang đôi khi cũng chứa tên cuộc trò chuyện.
    try:
        title = driver.title or ""
        if title:
            hints.append(title)
    except Exception:
        pass

    return " | ".join(x for x in hints if x)


def confirm_recipient(candidate_name, chat_box, timeout=2.2):
    expected = norm_text(first_meaningful_line(candidate_name))
    if len(expected) < 2:
        return False, ""

    end = time.time() + timeout
    last = ""

    while time.time() < end:
        last = get_active_chat_state(chat_box)
        hay = norm_text(last)

        if expected and expected in hay:
            return True, last

        # Kiểm tra token tên, nhưng chỉ khi token đủ dài.
        tokens = [t for t in expected.split() if len(t) >= 3]
        if len(tokens) >= 2 and all(t in hay for t in tokens[:3]):
            return True, last

        time.sleep(0.08)

    return False, last


# -----------------------------------------------------------------------------
# COMPOSER / SEND
# -----------------------------------------------------------------------------
def get_composer_text(chat_box):
    try:
        return driver.execute_script(
            """
            const el = arguments[0];
            return (el.innerText || el.textContent || '').replace(/\\u00a0/g, ' ');
            """,
            chat_box,
        ) or ""
    except Exception:
        return ""


def clear_composer(chat_box):
    """Focus thật vào composer rồi xóa nội dung draft cũ."""
    try:
        driver.execute_script(
            """
            const el = arguments[0];
            el.focus();
            try { document.getSelection()?.removeAllRanges(); } catch(e) {}
            """,
            chat_box,
        )
    except Exception:
        pass

    try:
        ActionChains(driver).move_to_element(chat_box).click().perform()
    except Exception:
        try:
            chat_box.click()
        except Exception:
            pass

    try:
        ActionChains(driver).key_down(Keys.CONTROL).send_keys('a').key_up(Keys.CONTROL).send_keys(Keys.BACKSPACE).perform()
    except Exception:
        try:
            chat_box.send_keys(Keys.CONTROL + 'a')
            chat_box.send_keys(Keys.BACK_SPACE)
        except Exception:
            pass


def type_message(chat_box, message):
    """Nhập tin nhắn ổn định cho contenteditable của Zalo.

    Ưu tiên thao tác bàn phím thật; nếu Zalo không cập nhật model DOM thì
    dùng execCommand/selection + InputEvent như fallback. Không dùng
    driver.execute_script để bấm gửi.
    """
    message = str(message)

    try:
        driver.execute_script("arguments[0].focus();", chat_box)
    except Exception:
        pass

    # Cách 1: bàn phím thật.
    try:
        parts = message.split("\\n")
        for i, part in enumerate(parts):
            if part:
                chat_box.send_keys(part)
            if i < len(parts) - 1:
                ActionChains(driver).key_down(Keys.SHIFT).send_keys(Keys.ENTER).key_up(Keys.SHIFT).perform()
        return
    except Exception:
        pass

    # Cách 2: click + send_keys toàn chuỗi.
    try:
        ActionChains(driver).move_to_element(chat_box).click().send_keys(message).perform()
        return
    except Exception:
        pass


def set_composer_text_js(chat_box, message):
    """Fallback mạnh cho Zalo contenteditable khi send_keys không đưa text vào.

    Không coi việc đổi textContent là đủ; phát cả input event để framework của
    Zalo nhận thay đổi. Hàm chỉ trả True khi đọc lại được đúng nội dung.
    """
    message = str(message)
    try:
        result = driver.execute_script(
            """
            const el = arguments[0];
            const text = arguments[1];
            el.focus();

            try {
                const sel = window.getSelection();
                const range = document.createRange();
                range.selectNodeContents(el);
                sel.removeAllRanges();
                sel.addRange(range);
            } catch (e) {}

            let inserted = false;
            try {
                inserted = !!document.execCommand('insertText', false, text);
            } catch (e) {}

            if (!inserted || ((el.innerText || el.textContent || '').trim() !== text.trim())) {
                el.textContent = '';
                const lines = text.split('\\n');
                for (let i = 0; i < lines.length; i++) {
                    if (i > 0) el.appendChild(document.createElement('br'));
                    el.appendChild(document.createTextNode(lines[i]));
                }
            }

            try {
                el.dispatchEvent(new InputEvent('input', {
                    bubbles: true,
                    inputType: 'insertText',
                    data: text
                }));
            } catch (e) {
                el.dispatchEvent(new Event('input', { bubbles: true }));
            }
            el.dispatchEvent(new Event('change', { bubbles: true }));

            return (el.innerText || el.textContent || '').replace(/\\u00a0/g, ' ').trim();
            """,
            chat_box,
            message,
        ) or ""
        return norm_text(result) == norm_text(message)
    except Exception:
        return False


def verify_composer_message(chat_box, message, timeout=None):
    expected_raw = str(message).replace("\\r\\n", "\\n").strip()
    expected = norm_text(expected_raw)
    deadline = time.time() + (timeout if timeout is not None else COMPOSER_VERIFY_TIMEOUT)
    last = ""

    while time.time() < deadline:
        last = norm_text(get_composer_text(chat_box))
        if last == expected:
            return True, last
        time.sleep(0.05)

    # Fallback 1: chèn trực tiếp vào contenteditable.
    if set_composer_text_js(chat_box, expected_raw):
        last = norm_text(get_composer_text(chat_box))
        if last == expected:
            return True, last

    # Fallback 2: nhập lại bằng keyboard sau khi clear.
    try:
        clear_composer(chat_box)
        driver.execute_script("arguments[0].focus();", chat_box)
        parts = expected_raw.split("\\n")
        for i, part in enumerate(parts):
            if part:
                chat_box.send_keys(part)
            if i < len(parts) - 1:
                ActionChains(driver).key_down(Keys.SHIFT).send_keys(Keys.ENTER).key_up(Keys.SHIFT).perform()
        time.sleep(0.05)
    except Exception:
        pass

    last = norm_text(get_composer_text(chat_box))
    return last == expected, last


def find_send_button():
    selectors = (
        "button[aria-label='Gửi']",
        "button[title='Gửi']",
        "[data-testid='send-button']",
        ".send-msg-btn",
        "[class*='send-msg']",
    )

    for sel in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if is_displayed(el) and is_enabled(el):
                    return el
        except Exception:
            pass
    return None


def _respect_send_interval():
    """Giãn tối thiểu giữa hai lần bấm gửi.

    Mục tiêu là tối ưu thời gian tìm/mở chat chứ không bắn tin dồn dập.
    Có thể chỉnh bằng biến môi trường ZALO_MIN_SEND_INTERVAL_SECONDS.
    """
    global _last_send_trigger_ts
    now = time.time()
    wait = MIN_SEND_INTERVAL_SECONDS - (now - _last_send_trigger_ts)
    if wait > 0:
        time.sleep(wait)
    _last_send_trigger_ts = time.time()


def trigger_send(chat_box):
    _respect_send_interval()

    button = find_send_button()
    if button is not None and safe_click(button):
        return "button"

    # Zalo fallback: Enter trong composer.
    try:
        chat_box.send_keys(Keys.ENTER)
        return "enter"
    except Exception:
        return "failed"


def composer_is_empty(chat_box):
    return not norm_text(get_composer_text(chat_box))


def wait_send_confirmed(chat_box, message, timeout=2.5):
    """Composer rỗng hoặc message xuất hiện ở vùng chat."""
    expected = norm_text(message)
    end = time.time() + timeout

    while time.time() < end:
        if composer_is_empty(chat_box):
            return True

        # Tìm message ở nửa phải, giới hạn cuối trang.
        try:
            found = driver.execute_script(
                """
                const expected = arguments[0];
                const W = window.innerWidth, H = window.innerHeight;
                const els = [];
                for (const el of document.querySelectorAll('div,span,p')) {
                    const r = el.getBoundingClientRect();
                    if (!r.width || !r.height) continue;
                    if (r.left < W * .35 || r.top < H * .45) continue;
                    if (r.width > W * .8 || r.height > 220) continue;

                    const t = (el.innerText || el.textContent || '')
                        .replace(/\\s+/g,' ').trim().toLocaleLowerCase('vi-VN');
                    if (!t || t.length > 500) continue;
                    if (t === expected || t.includes(expected)) {
                        els.push({text:t, y:r.top});
                    }
                }
                els.sort((a,b) => b.y-a.y);
                return els.length > 0;
                """,
                expected,
            )
            if found:
                return True
        except Exception:
            pass

        time.sleep(0.08)

    return False


# -----------------------------------------------------------------------------
# OPEN / FIND / SEND
# -----------------------------------------------------------------------------
def _click_profile_message_button():
    """Nếu click kết quả tìm kiếm mở card hồ sơ thay vì chat, bấm nút Nhắn tin."""
    selectors = (
        "button",
        "[role='button']",
        "a",
        "div",
    )
    accepted = {"nhắn tin", "gửi tin nhắn", "message", "chat"}
    try:
        for sel in selectors:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if not is_displayed(el) or not is_enabled(el):
                    continue
                txt = norm_text(element_text(el))
                if txt not in accepted:
                    continue
                try:
                    r = el.rect
                    if r.get("width", 0) < 45 or r.get("height", 0) < 24:
                        continue
                    if r.get("width", 0) > 320 or r.get("height", 0) > 90:
                        continue
                except Exception:
                    pass
                if safe_click(el):
                    print(f"[ZALO] Đã bấm nút hồ sơ: {txt!r}")
                    return True
    except Exception:
        pass
    return False


def _wait_target_header(candidate_name, timeout=1.2):
    confirmed, hint = confirm_recipient(candidate_name, None, timeout=timeout)
    return confirmed, hint


def _click_candidate_row(candidate, candidate_name=""):
    """Click phần tử thật sự tương tác bên trong card kết quả.

    Zalo Web đôi khi trả về một div bao ngoài không có handler click. Nếu có tên
    người dùng bên trong, ta ưu tiên phần tử/ancestor có role/button/tabindex gần
    tên đó; cuối cùng mới fallback click card gốc.
    """
    expected = norm_text(candidate_name)
    if expected:
        try:
            clickable = driver.execute_script(
                """
                const root = arguments[0];
                const expected = arguments[1] || '';
                const norm = (v) => String(v || '')
                    .normalize('NFC').toLocaleLowerCase('vi-VN')
                    .replace(/\\s+/g, ' ').trim();
                const visible = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return !!(r.width && r.height && st.display !== 'none' &&
                        st.visibility !== 'hidden' && st.opacity !== '0');
                };
                const all = [...root.querySelectorAll('*'), root];
                for (const el of all) {
                    if (!visible(el)) continue;
                    const own = norm(el.innerText || el.textContent || '');
                    if (!(own === expected || own.startsWith(expected + ' '))) continue;
                    let p = el;
                    for (let i=0; i<4 && p; i++, p=p.parentElement) {
                        if (!visible(p)) continue;
                        const r = p.getBoundingClientRect();
                        if (r.height > 160 || r.width < 100) continue;
                        const role = (p.getAttribute('role') || '').toLowerCase();
                        const tag = (p.tagName || '').toLowerCase();
                        const tabindex = p.getAttribute('tabindex');
                        const cls = String(p.className || '').toLowerCase();
                        if (tag === 'a' || tag === 'button' || role === 'button' ||
                            role === 'option' || role === 'listitem' || tabindex !== null ||
                            /friend|contact|search-result|suggest|item/.test(cls)) {
                            return p;
                        }
                    }
                    return el;
                }
                return null;
                """,
                candidate,
                expected,
            )
            if clickable is not None and is_displayed(clickable) and safe_click(clickable):
                return True
        except Exception:
            pass

    return safe_click(candidate)


def click_candidate_and_get_chat(candidate, candidate_name):
    """Mở đúng người nhận rồi mới lấy composer.

    Điểm sửa quan trọng: composer của cuộc chat CŨ luôn vẫn tồn tại khi panel tìm kiếm
    đang mở. Vì vậy tuyệt đối không coi "thấy ô nhập" là đã mở đúng phụ huynh.
    Phải xác nhận header đổi sang candidate_name trước; nếu Zalo mở card hồ sơ thì
    bấm "Nhắn tin" rồi xác nhận lại.
    """
    before_header = get_active_chat_header_hint()
    if not _click_candidate_row(candidate, candidate_name):
        return None, "Không click được contact."

    deadline = time.time() + CHAT_INPUT_TIMEOUT
    profile_button_attempts = 0
    next_profile_try = time.time() + 0.18
    last_hint = before_header

    while time.time() < deadline:
        confirmed, last_hint = _wait_target_header(candidate_name, timeout=0.20)
        if confirmed:
            break

        # Kết quả "Tìm bạn qua số điện thoại" thường mở card hồ sơ trước.
        # Nút "Nhắn tin" có thể render trễ 0.2-1.0 giây, vì vậy thử lại có giới hạn
        # thay vì chỉ thử đúng một lần ngay sau khi click.
        now = time.time()
        if profile_button_attempts < 5 and now >= next_profile_try:
            profile_button_attempts += 1
            clicked_profile_message = _click_profile_message_button()
            next_profile_try = now + (0.35 if not clicked_profile_message else 0.55)

        time.sleep(0.06)
    else:
        return None, (
            f"Click kết quả nhưng chat chưa chuyển sang {candidate_name!r}. "
            f"Header trước={before_header!r}; hiện tại={last_hint!r}"
        )

    # Đúng người nhận rồi mới đóng panel search và lấy composer mới.
    close_search_overlay()
    chat_box = find_visible_chat_input(timeout=1.8)
    if chat_box is None:
        return None, "Đã mở đúng người nhận nhưng không tìm thấy ô nhập tin nhắn."

    confirmed, hint = confirm_recipient(
        candidate_name,
        chat_box,
        timeout=min(1.4, RECIPIENT_CONFIRM_TIMEOUT),
    )
    if not confirmed:
        return None, f"Không xác nhận được người nhận sau khi mở chat. UI={hint}"

    return chat_box, ""



def open_recipient(phone, allow_global=True, allow_contact=True):
    target = normalize_vietnam_phone(phone)
    if not target:
        return None, "", "", "INVALID_PHONE"

    # A. Chat/contact đang hiển thị và chứa đúng số.
    direct = find_visible_phone_row(target)
    if direct is not None:
        name = extract_candidate_name(direct, target)
        if name:
            chat_box, err = click_candidate_and_get_chat(direct, name)
            if chat_box is not None:
                return chat_box, name, "visible_phone_row", ""
            print("[ZALO] Direct row không xác nhận:", err)

    # B. Nếu search box đã có đúng target + result đã hiện thì dùng luôn.
    if allow_global:
        search = find_zalo_search_box(prefer_contact=False, timeout=SEARCH_BOX_TIMEOUT)
        if search is not None:
            if is_search_box_value(search, target):
                candidate = _find_phone_friend_result(search, target)
                if candidate is None:
                    candidate = _find_left_search_candidate(search, target_phone=target)
                if candidate is None:
                    candidate = _find_search_result_row(search, target)
                if candidate is not None:
                    name = extract_candidate_name(candidate, target)
                    if name:
                        # Nếu chat hiện tại đã đúng tên thì không click lại; chỉ đóng overlay.
                        current = norm_text(get_active_chat_header_hint())
                        if current and norm_text(name) in current:
                            close_search_overlay()
                            box = find_visible_chat_input(timeout=0.8)
                            if box is not None:
                                confirmed, hint = confirm_recipient(name, box, timeout=0.8)
                                if confirmed:
                                    return box, name, "existing_search_result", ""

                        box, err = click_candidate_and_get_chat(candidate, name)
                        if box is not None:
                            return box, name, "global_existing_search", ""
                        print("[ZALO] Existing-search candidate không xác nhận:", err)

            # C. Global search bình thường. Ưu tiên đúng định dạng 0xxxxxxxxx.
            # Không thử nhiều biến thể liên tiếp nếu không cần, giúp nhanh hơn và giảm thao tác thừa.
            for attempt in range(max(1, SEARCH_RETRY_COUNT)):
                query_list = (target,) if attempt == 0 else ("+84" + target[1:],)
                for query in query_list:
                    candidate = search_once(search, query, target, timeout=SEARCH_RESULT_TIMEOUT)
                    if candidate is not None:
                        name = extract_candidate_name(candidate, target)
                        if name:
                            box, err = click_candidate_and_get_chat(candidate, name)
                            if box is not None:
                                return box, name, "global", ""
                            print("[ZALO] Global candidate không xác nhận:", err)
                    clear_search_box(search)

            try:
                search.send_keys(Keys.ESCAPE)
            except Exception:
                pass

    # D. Danh bạ.
    if allow_contact:
        try:
            tab = WebDriverWait(driver, 1.0).until(
                EC.element_to_be_clickable(
                    (
                        By.CSS_SELECTOR,
                        "[title='Danh bạ'],[title='Contacts'],"
                        "[aria-label='Danh bạ'],i[icon*='Contact']",
                    )
                )
            )
            safe_click(tab)
        except Exception:
            pass

        search = find_zalo_search_box(prefer_contact=True, timeout=SEARCH_BOX_TIMEOUT)
        if search is not None:
            is_contact = (
                (search.get_attribute("id") or "").lower() == "contact-search-input"
                or (search.get_attribute("name") or "").lower() == "contact-search"
            )
            if is_contact:
                for query in (target, "+84" + target[1:]):
                    candidate = search_once(search, query, target, timeout=SEARCH_RESULT_TIMEOUT)
                    if candidate is not None:
                        name = extract_candidate_name(candidate, target)
                        if name:
                            box, err = click_candidate_and_get_chat(candidate, name)
                            if box is not None:
                                return box, name, "contact", ""
                            print("[ZALO] Contact candidate không xác nhận:", err)
                    clear_search_box(search)

    return None, "", "", "NOT_FOUND"



def open_recipient_by_name(zalo_name):
    """Fallback theo Tên Zalo; chỉ mở khi có đúng 1 kết quả khớp chính xác."""
    expected = (zalo_name or "").strip()
    if len(norm_text(expected)) < 2:
        return None, "", "", "INVALID_ZALO_NAME"

    close_search_overlay()

    search = find_zalo_search_box(prefer_contact=False, timeout=SEARCH_BOX_TIMEOUT)
    if search is not None:
        candidate, status = search_name_once(search, expected, timeout=SEARCH_RESULT_TIMEOUT)
        if status == "AMBIGUOUS_ZALO_NAME":
            clear_search_box(search)
            return None, "", "", status
        if candidate is not None:
            name = extract_candidate_name(candidate)
            box, err = click_candidate_and_get_chat(candidate, name)
            if box is not None:
                return box, name, "zalo_name_global", ""
            print("[ZALO] Name-global candidate không xác nhận:", err)
        clear_search_box(search)
        try:
            search.send_keys(Keys.ESCAPE)
        except Exception:
            pass

    try:
        tab = WebDriverWait(driver, 1.0).until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "[title='Danh bạ'],[title='Contacts'],[aria-label='Danh bạ'],i[icon*='Contact']")
            )
        )
        safe_click(tab)
    except Exception:
        pass

    search = find_zalo_search_box(prefer_contact=True, timeout=SEARCH_BOX_TIMEOUT)
    if search is not None:
        candidate, status = search_name_once(search, expected, timeout=SEARCH_RESULT_TIMEOUT)
        if status == "AMBIGUOUS_ZALO_NAME":
            clear_search_box(search)
            return None, "", "", status
        if candidate is not None:
            name = extract_candidate_name(candidate)
            box, err = click_candidate_and_get_chat(candidate, name)
            if box is not None:
                return box, name, "zalo_name_contact", ""
            print("[ZALO] Name-contact candidate không xác nhận:", err)
        clear_search_box(search)

    return None, "", "", "NOT_FOUND"



def send_message_to_phone(phone, message, zalo_name="", notify_on_failure=False, reason_context=None):
    """Gửi một tin nhắn qua Zalo.

    notify_on_failure/ reason_context chỉ giữ để tương thích API cũ.
    Việc gửi cảnh báo lỗi được thực hiện ở tầng API SAU khi release selenium_lock,
    tránh deadlock khi cảnh báo cũng cần dùng Selenium.
    """
    global driver

    if driver is None:
        return {
            "success": False,
            "status": "BOT_OFFLINE",
            "message": "Bot chưa khởi động thành công.",
        }

    target = normalize_vietnam_phone(phone)
    clean_zalo_name = (zalo_name or "").strip()
    if not target and not norm_text(clean_zalo_name):
        return {
            "success": False,
            "status": "NO_RECIPIENT_KEY",
            "message": "Không có SĐT hợp lệ hoặc Tên Zalo để tìm người nhận.",
        }

    clean_self = normalize_vietnam_phone(ZALO_LOGGED_IN_PHONE)
    if target and target == clean_self:
        return {
            "success": False,
            "status": "SELF_ACCOUNT",
            "message": "Không thể gửi cho chính tài khoản đang đăng nhập.",
        }

    send_triggered = False
    search_mode = ""
    candidate_name = ""

    try:
        driver.switch_to.default_content()

        # Đảm bảo search overlay cũ không giữ focus.
        close_search_overlay()

        chat_box = None
        candidate_name = ""
        search_mode = ""
        err = "NOT_FOUND"

        # Ưu tiên tuyệt đối SĐT khi có SĐT hợp lệ.
        if target:
            # Nếu đã có Tên Zalo, chỉ thử SĐT ở global search trước.
            # Nếu thất bại sẽ chuyển ngay sang tên, tránh mất thời gian quét Danh bạ theo SĐT.
            chat_box, candidate_name, search_mode, err = open_recipient(
                target, allow_global=True, allow_contact=not bool(norm_text(clean_zalo_name))
            )

        # Chỉ fallback sang tên khi không có SĐT hợp lệ hoặc tìm theo SĐT thất bại.
        if chat_box is None and norm_text(clean_zalo_name):
            if target:
                print(f"[ZALO] Không tìm được theo SĐT {target}; thử Tên Zalo={clean_zalo_name!r}")
            else:
                print(f"[ZALO] Không có SĐT hợp lệ; thử Tên Zalo={clean_zalo_name!r}")
            chat_box, candidate_name, search_mode, name_err = open_recipient_by_name(clean_zalo_name)
            if chat_box is None and name_err == "AMBIGUOUS_ZALO_NAME":
                return {
                    "success": False,
                    "status": "AMBIGUOUS_ZALO_NAME",
                    "message": f"Có nhiều tài khoản cùng tên Zalo '{clean_zalo_name}', đã hủy để tránh gửi nhầm.",
                    "phone": target,
                    "zalo_name": clean_zalo_name,
                    "search_mode": "zalo_name",
                }
            if chat_box is None:
                err = name_err or err

        if chat_box is None:
            return {
                "success": False,
                "status": "NOT_FOUND" if err == "NOT_FOUND" else "RECIPIENT_NOT_CONFIRMED",
                "message": (
                    f"Không tìm thấy tài khoản/đoạn chat cho SĐT {target}"
                    + (f" hoặc Tên Zalo '{clean_zalo_name}'." if clean_zalo_name else ".")
                    if err == "NOT_FOUND"
                    else f"Không mở/xác nhận được người nhận {target or clean_zalo_name}."
                ),
                "phone": target,
                "zalo_name": clean_zalo_name,
                "search_mode": search_mode,
            }

        print(
            f"[ZALO] Candidate={candidate_name!r}, mode={search_mode}, phone={target or '-'}"
        )

        # Composer phải nằm ở pane chat, không phải search.
        close_search_overlay()
        chat_box = find_visible_chat_input(timeout=0.8) or chat_box
        clear_composer(chat_box)
        type_message(chat_box, message)

        ok, composer_value = verify_composer_message(
            chat_box, message, timeout=COMPOSER_VERIFY_TIMEOUT
        )
        if not ok:
            clear_composer(chat_box)
            return {
                "success": False,
                "status": "COMPOSER_MISMATCH",
                "message": "Đã mở đúng người nhận nhưng nội dung chưa vào đúng ô chat; đã hủy gửi.",
                "phone": target,
                "recipient": candidate_name,
                "composer_value": composer_value[:300],
                "search_mode": search_mode,
            }

        # Xác nhận người nhận lần cuối ngay trước khi send.
        confirmed2, hint2 = confirm_recipient(
            candidate_name, chat_box, timeout=min(1.0, RECIPIENT_CONFIRM_TIMEOUT)
        )
        if not confirmed2:
            clear_composer(chat_box)
            return {
                "success": False,
                "status": "RECIPIENT_CHANGED",
                "message": "Người nhận trên giao diện đã thay đổi trước khi gửi.",
                "phone": target,
                "recipient": candidate_name,
                "ui_hint": hint2,
                "search_mode": search_mode,
            }

        # Đảm bảo focus nằm trong composer trước Enter.
        try:
            driver.execute_script("arguments[0].focus();", chat_box)
        except Exception:
            pass

        method = trigger_send(chat_box)
        if method == "failed":
            return {
                "success": False,
                "status": "SEND_TRIGGER_FAILED",
                "message": "Không kích hoạt được thao tác gửi.",
                "phone": target,
                "recipient": candidate_name,
                "search_mode": search_mode,
            }

        send_triggered = True

        if wait_send_confirmed(chat_box, message, timeout=SEND_CONFIRM_TIMEOUT):
            return {
                "success": True,
                "status": "SENT",
                "message": "Đã gửi.",
                "phone": target,
                "recipient": candidate_name,
                "send_method": method,
                "search_mode": search_mode,
            }

        # Đã trigger nhưng chưa xác nhận => không retry.
        return {
            "success": True,
            "status": "SENT_UNCONFIRMED",
            "message": "Đã kích hoạt gửi nhưng UI chưa xác nhận được.",
            "phone": target,
            "recipient": candidate_name,
            "send_method": method,
            "search_mode": search_mode,
            "debug_screenshot": save_debug_screenshot("sent_unconfirmed"),
        }

    except Exception as e:
        print("[ZALO][ERROR]", repr(e))
        traceback.print_exc()

        if send_triggered:
            return {
                "success": True,
                "status": "SENT_UNCONFIRMED",
                "message": "Đã kích hoạt gửi, Selenium lỗi sau đó; không retry.",
                "phone": target,
                "recipient": candidate_name,
                "search_mode": search_mode,
                "error": str(e),
                "debug_screenshot": save_debug_screenshot("sent_exception"),
            }

        return {
            "success": False,
            "status": "SELENIUM_ERROR",
            "message": f"Lỗi Selenium trước khi gửi: {str(e)}",
            "phone": target,
            "recipient": candidate_name,
            "search_mode": search_mode,
            "debug_screenshot": save_debug_screenshot("selenium_exception"),
        }



# -----------------------------------------------------------------------------
# FAILURE NOTICE
# -----------------------------------------------------------------------------
def _build_failure_notice(target_phone, student_name="", class_name="", student_id="", recipient_type="parent", zalo_name=""):
    """Tạo cảnh báo có đủ ngữ cảnh học sinh/lớp.

    Tên/lớp được lấy từ payload Apps Script; student_id dùng làm dấu vết cuối cùng
    nếu một request rất cũ không có đủ metadata.
    """
    student_name = _clean_context_value(student_name)
    class_name = _clean_context_value(class_name)
    student_id = _clean_context_value(student_id)
    target_phone = _clean_context_value(target_phone) or "[không có SĐT]"
    recipient_type = _clean_context_value(recipient_type).casefold()
    zalo_name = _clean_context_value(zalo_name)

    if recipient_type == "group":
        group_label = zalo_name or student_name or class_name or "[không rõ tên nhóm]"
        return f"Không tìm thấy hoặc không mở được nhóm Zalo '{group_label}' để gửi báo cáo số buổi học."

    if student_name and class_name:
        return f"Không tìm thấy Zalo của phụ huynh em {student_name} Lớp {class_name}, số điện thoại: {target_phone}."
    if student_name:
        suffix = f" (Mã HS: {student_id})" if student_id else ""
        return f"Không tìm thấy Zalo của phụ huynh em {student_name}{suffix}, số điện thoại: {target_phone}."
    if student_id:
        return f"Không tìm thấy Zalo của phụ huynh học sinh mã {student_id}, số điện thoại: {target_phone}."
    return f"Không tìm thấy Zalo của phụ huynh, số điện thoại: {target_phone}. Dữ liệu gửi lên thiếu tên học sinh/lớp."


def _send_notice_transaction(notify_phone, notice_text):
    """Transaction riêng cho tin cảnh báo; không bao giờ gọi lại send_failure_notice()."""
    target = normalize_vietnam_phone(notify_phone)
    if not target or driver is None:
        return {
            "success": False,
            "status": "NOTICE_BOT_OFFLINE" if driver is None else "INVALID_FAILURE_NOTIFY_PHONE",
            "phone": notify_phone,
        }

    send_triggered = False
    candidate_name = ""
    search_mode = ""

    try:
        driver.switch_to.default_content()

        # Không đóng search ngay nếu nó đang hiển thị đúng số cảnh báo.
        search_box = find_zalo_search_box(prefer_contact=False, timeout=1.5)
        candidate = None
        if search_box is not None and is_search_box_value(search_box, target):
            candidate = _find_search_result_row(search_box, target)
            if candidate is not None:
                candidate_name = extract_candidate_name(candidate, target)
                if candidate_name:
                    search_mode = "existing_notice_search"
                    chat_box, err = click_candidate_and_get_chat(candidate, candidate_name)
                    if chat_box is not None:
                        return _finish_notice_send(chat_box, candidate_name, search_mode, notice_text)
                    print("[ZALO][NOTICE] Existing search result không mở được:", err)

        # Search bình thường.
        chat_box, candidate_name, search_mode, err = open_recipient(
            target, allow_global=True, allow_contact=True
        )
        if chat_box is None:
            return {
                "success": False,
                "status": "NOTICE_RECIPIENT_NOT_FOUND",
                "message": f"Không mở được tài khoản nhận cảnh báo {target}: {err}",
                "phone": target,
            }

        return _finish_notice_send(chat_box, candidate_name, search_mode, notice_text)

    except Exception as exc:
        print("[ZALO][NOTICE][ERROR]", repr(exc))
        traceback.print_exc()
        return {
            "success": False,
            "status": "NOTICE_SEND_EXCEPTION" if not send_triggered else "NOTICE_SENT_UNCONFIRMED",
            "message": str(exc),
            "phone": target,
            "recipient": candidate_name,
            "search_mode": search_mode,
        }


def _finish_notice_send(chat_box, candidate_name, search_mode, notice_text):
    """Đưa nội dung cảnh báo vào composer và gửi, với kiểm tra chặt chẽ."""
    close_search_overlay()
    fresh = find_visible_chat_input(timeout=1.0)
    if fresh is not None:
        chat_box = fresh

    confirmed, hint = confirm_recipient(candidate_name, chat_box, timeout=1.5)
    if not confirmed:
        return {
            "success": False,
            "status": "NOTICE_RECIPIENT_NOT_CONFIRMED",
            "message": f"Không xác nhận được người nhận cảnh báo: {candidate_name}",
            "phone": ZALO_FAILURE_NOTIFY_PHONE,
            "recipient": candidate_name,
            "ui_hint": hint,
            "search_mode": search_mode,
        }

    clear_composer(chat_box)
    # Dùng cả keyboard và JS fallback; không trả success nếu composer rỗng.
    type_message(chat_box, notice_text)
    ok, current = verify_composer_message(
        chat_box, notice_text, timeout=0.8
    )
    if not ok:
        # Một lần nữa bằng JS sau khi clear, dành riêng cho alert ngắn.
        clear_composer(chat_box)
        ok = set_composer_text_js(chat_box, notice_text)
        current = get_composer_text(chat_box)

    if not ok or norm_text(current) != norm_text(notice_text):
        return {
            "success": False,
            "status": "NOTICE_COMPOSER_FAILED",
            "message": "Không đưa được đầy đủ nội dung cảnh báo vào ô chat.",
            "phone": ZALO_FAILURE_NOTIFY_PHONE,
            "recipient": candidate_name,
            "composer_value": current[:500],
            "search_mode": search_mode,
        }

    confirmed2, hint2 = confirm_recipient(
        candidate_name, chat_box, timeout=0.8
    )
    if not confirmed2:
        clear_composer(chat_box)
        return {
            "success": False,
            "status": "NOTICE_RECIPIENT_CHANGED",
            "message": "Người nhận cảnh báo thay đổi trước khi gửi.",
            "phone": ZALO_FAILURE_NOTIFY_PHONE,
            "recipient": candidate_name,
            "ui_hint": hint2,
            "search_mode": search_mode,
        }

    try:
        driver.execute_script("arguments[0].focus();", chat_box)
    except Exception:
        pass

    method = trigger_send(chat_box)
    if method == "failed":
        return {
            "success": False,
            "status": "NOTICE_SEND_TRIGGER_FAILED",
            "message": "Không kích hoạt được thao tác gửi cảnh báo.",
            "phone": ZALO_FAILURE_NOTIFY_PHONE,
            "recipient": candidate_name,
            "search_mode": search_mode,
        }

    # Đã kích hoạt gửi -> tuyệt đối không retry để tránh gửi trùng.
    if wait_send_confirmed(chat_box, notice_text, timeout=NOTICE_SEND_TIMEOUT):
        return {
            "success": True,
            "status": "NOTICE_SENT",
            "message": "Đã gửi cảnh báo.",
            "phone": ZALO_FAILURE_NOTIFY_PHONE,
            "recipient": candidate_name,
            "send_method": method,
            "search_mode": search_mode,
            "notice_text": notice_text,
        }

    return {
        "success": True,
        "status": "NOTICE_SENT_UNCONFIRMED",
        "message": "Đã kích hoạt gửi cảnh báo nhưng chưa xác nhận được UI.",
        "phone": ZALO_FAILURE_NOTIFY_PHONE,
        "recipient": candidate_name,
        "send_method": method,
        "search_mode": search_mode,
        "notice_text": notice_text,
        "debug_screenshot": save_debug_screenshot("notice_sent_unconfirmed"),
    }


def send_failure_notice(target_phone, reason, original_message="", reason_context=None, student_name=None, class_name=None, student_id=None, recipient_type="parent", zalo_name=""):
    """Gửi cảnh báo tới 0986108104 bằng transaction Selenium riêng.

    Nội dung được rút gọn theo yêu cầu nghiệp vụ:
    'Không tìm thấy Zalo của phụ huynh em <tên học sinh> Lớp <tên lớp>, số điện thoại: <SĐT>.'
    """
    notify_phone = normalize_vietnam_phone(ZALO_FAILURE_NOTIFY_PHONE)
    target = normalize_vietnam_phone(target_phone)

    if not notify_phone:
        return {
            "success": False,
            "status": "INVALID_FAILURE_NOTIFY_PHONE",
            "phone": ZALO_FAILURE_NOTIFY_PHONE,
        }

    # Hỗ trợ cả chuỗi reason_context cũ và dict mới.
    if isinstance(reason_context, dict):
        student_name = student_name or reason_context.get("student_name") or reason_context.get("ten_hoc_sinh")
        class_name = class_name or reason_context.get("class_name") or reason_context.get("ten_lop")
        student_id = student_id or reason_context.get("student_id") or reason_context.get("ma_hoc_sinh")
        recipient_type = reason_context.get("recipient_type") or recipient_type
        zalo_name = reason_context.get("zalo_name") or reason_context.get("ten_zalo") or zalo_name
    elif reason_context and not student_name:
        student_name = str(reason_context).strip()

    # Một lớp fallback cuối cho request cũ.
    if not student_name:
        student_name = _infer_student_name_from_message(original_message)
    if not class_name:
        class_name = _infer_class_name_from_message(original_message)

    notice = _build_failure_notice(target or target_phone, student_name, class_name, student_id, recipient_type, zalo_name)

    print(
        f"[ZALO][FAILURE_NOTICE] gửi tới={notify_phone} "
        f"phụ_huynh={target or target_phone} học_sinh={student_name or '-'} "
        f"lớp={class_name or '-'}"
    )

    last_result = None
    # Tối đa 2 transaction khi chưa hề trigger gửi. Không retry khi đã trigger.
    for attempt in range(max(1, NOTICE_SEND_RETRY_COUNT)):
        try:
            with selenium_lock:
                result = _send_notice_transaction(notify_phone, notice)
            last_result = result
            print(
                f"[ZALO][NOTICE] attempt={attempt + 1} "
                f"status={result.get('status')} recipient={result.get('recipient')}"
            )
            if result.get("success"):
                return result
            if result.get("status") in {
                "NOTICE_SENT_UNCONFIRMED",
                "NOTICE_SEND_EXCEPTION",
                "NOTICE_RECIPIENT_CHANGED",
            }:
                return result
        except Exception as exc:
            last_result = {
                "success": False,
                "status": "NOTICE_SEND_EXCEPTION",
                "message": str(exc),
                "phone": notify_phone,
            }
            print("[ZALO][NOTICE][ERROR]", repr(exc))

    return last_result or {
        "success": False,
        "status": "NOTICE_SEND_FAILED",
        "phone": notify_phone,
        "notice_text": notice,
    }


# -----------------------------------------------------------------------------
# REQUEST CONTEXT - lấy chắc chắn tên học sinh/lớp từ Apps Script
# -----------------------------------------------------------------------------
def _clean_context_value(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _first_context_value(data, nested, *keys):
    for source in (data, nested):
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = _clean_context_value(source.get(key))
            if value:
                return value
    return ""


def _infer_student_name_from_message(message):
    """Fallback cho request cũ chỉ có nội dung tin nhắn.

    Các mẫu hiện tại của hệ thống đều chứa 'Học sinh <tên> đã/hiện...' hoặc
    '... của học sinh <tên>, số tiền ...'. Chỉ dùng khi payload không có tên.
    """
    text = _clean_context_value(message)
    if not text:
        return ""
    patterns = [
        r"(?i)\bhọc\s+sinh\s+(.+?)(?=\s+(?:đã|hiện|chưa|thuộc)\b|[,.;:\n]|$)",
        r"(?i)\bem\s+(.+?)(?=\s+(?:đã|hiện|chưa|thuộc)\b|[,.;:\n]|$)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            value = _clean_context_value(m.group(1))
            if 2 <= len(value) <= 120:
                return value
    return ""


def _infer_class_name_from_message(message):
    text = _clean_context_value(message)
    if not text:
        return ""
    # Chỉ chấp nhận mẫu lớp có số khối để không bắt nhầm cụm 'tại lớp vào ngày'.
    m = re.search(r"(?i)\blớp\s+K?\s*(\d{1,2}\s*[-.]?\s*[A-Za-z0-9]+)\b", text)
    if not m:
        return ""
    value = re.sub(r"\s+", "", m.group(1)).replace("-", "")
    return value[:40]


def _format_vnd_amount(value):
    """Chuẩn hóa số tiền học phí về dạng 1.500.000 đồng."""
    if value is None:
        return ""
    raw = _clean_context_value(value)
    if not raw:
        return ""

    # Trường hợp số từ JSON/Sheet bị thành 1500000.0.
    try:
        if isinstance(value, (int, float)):
            amount = int(round(float(value)))
        else:
            # Google Sheet/JSON đôi khi biến số thành chuỗi "1500000.0".
            # Không được bỏ dấu chấm rồi thành 15.000.000.
            if re.fullmatch(r"\d+\.0+", raw):
                amount = int(float(raw))
            else:
                # Hỗ trợ "1.500.000 đ", "1,500,000", "1500000".
                digits = re.sub(r"\D", "", raw)
                if not digits:
                    return ""
                amount = int(digits)
    except Exception:
        return ""

    if amount <= 0:
        return ""
    return f"{amount:,}".replace(",", ".") + " đồng"


def _looks_like_monthly_tuition_message(message):
    text = unicodedata.normalize("NFD", str(message or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn").casefold()
    text = re.sub(r"\s+", " ", text)
    return bool(
        re.search(r"\bhoc\s*phi\b", text)
        or re.search(r"\bhphi\b", text)
        or re.search(r"\btuition\b", text)
    )


def _message_already_has_money(message):
    text = str(message or "")
    return bool(
        re.search(r"\b\d[\d\s.,]{2,}\s*(?:đ|đồng|vnd)\b", text, flags=re.IGNORECASE)
        or re.search(r"(?i)số\s*tiền\s*(?:học\s*phí)?\s*[:\-]\s*\d", text)
    )


def enrich_monthly_tuition_message(message, tuition_amount="", tuition_month=""):
    """Bổ sung số tiền vào tin nhắc học phí tháng nếu payload có gửi số tiền.

    Không tự đoán số tiền. Nếu Apps Script chưa gửi một trong các trường tiền
    được hỗ trợ, nội dung giữ nguyên để tránh thông báo sai phụ huynh.
    """
    text = str(message or "").strip()
    if not text or not _looks_like_monthly_tuition_message(text):
        return text
    if _message_already_has_money(text):
        return text

    amount_text = _format_vnd_amount(tuition_amount)
    if not amount_text:
        return text

    month = _clean_context_value(tuition_month)
    if month:
        line = f"Số tiền học phí tháng {month}: {amount_text}."
    else:
        line = f"Số tiền học phí cần đóng: {amount_text}."
    return text.rstrip() + "\n" + line


def extract_request_context(data, message=""):
    data = data if isinstance(data, dict) else {}
    nested = data.get("context") if isinstance(data.get("context"), dict) else {}

    student_id = _first_context_value(data, nested, "student_id", "ma_hoc_sinh", "studentId")
    student_name = _first_context_value(
        data, nested,
        "ten_hoc_sinh", "ho_ten_hoc_sinh", "ho_ten_hs", "student_name", "hoc_sinh", "name"
    )
    class_name = _first_context_value(
        data, nested,
        "ten_lop", "lop", "ten_lop_hoc", "class_name", "class", "class_info"
    )
    zalo_name = _first_context_value(
        data, nested,
        "ten_zalo", "zalo_name", "ten_zalo_phu_huynh"
    )
    tuition_amount = _first_context_value(
        data, nested,
        "so_tien_hoc_phi", "tien_hoc_phi", "hoc_phi_thang", "hoc_phi",
        "so_tien", "amount", "tuition_amount", "tuition_fee", "fee_amount", "monthly_fee"
    )
    tuition_month = _first_context_value(
        data, nested,
        "thang_hoc_phi", "thang_thu_hoc_phi", "tuition_month", "fee_month", "month"
    )
    recipient_type = _first_context_value(
        data, nested, "recipient_type", "target_type", "loai_nguoi_nhan"
    ) or "parent"

    # Tương thích request/queue cũ: cố gắng suy ra tên từ nội dung.
    if not student_name:
        student_name = _infer_student_name_from_message(message)
    if not class_name:
        class_name = _infer_class_name_from_message(message)

    return {
        "student_id": student_id,
        "student_name": student_name,
        "class_name": class_name,
        "zalo_name": zalo_name,
        "tuition_amount": tuition_amount,
        "tuition_month": tuition_month,
        "recipient_type": recipient_type,
    }


# -----------------------------------------------------------------------------
# API
# -----------------------------------------------------------------------------
# V18: biên nhận bền vững, không gửi lại khi kết quả còn chưa rõ.
JOURNAL_PATH = os.getenv('ZALO_JOURNAL_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'zalo_receipts.sqlite3'))


def _journal_claim(key, fingerprint):
    with sqlite3.connect(JOURNAL_PATH, timeout=15) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS receipts (request_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, result TEXT, created_at TEXT NOT NULL)')
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute('SELECT fingerprint,status,result FROM receipts WHERE request_key=?', (key,)).fetchone()
        if existing:
            if existing[0] != fingerprint:
                return {'success': False, 'status': 'IDEMPOTENCY_CONFLICT', 'message': 'Mã yêu cầu đã được dùng cho nội dung/người nhận khác.'}
            if existing[2]:
                result = json.loads(existing[2])
                result['duplicate'] = True
                return result
            return {'success': False, 'status': 'RESULT_UNKNOWN', 'message': 'Yêu cầu đang xử lý hoặc chưa rõ kết quả; cần đối chiếu trên Zalo, không tự gửi lại.'}
        conn.execute('INSERT INTO receipts (request_key,fingerprint,status,created_at) VALUES (?,?,?,?)', (key, fingerprint, 'RESERVED', datetime.now().isoformat()))
    return None


def _journal_finish(key, result):
    with sqlite3.connect(JOURNAL_PATH, timeout=15) as conn:
        conn.execute('UPDATE receipts SET status=?,result=? WHERE request_key=?', (str(result.get('status', 'UNKNOWN')), json.dumps(result, ensure_ascii=False), key))


@app.before_request
def verify_gateway_token():
    if request.path == '/health':
        return None
    expected = os.getenv('ZALO_GATEWAY_TOKEN', '')
    if not expected:
        return jsonify({'success': False, 'status': 'TOKEN_NOT_CONFIGURED', 'message': 'Chưa cấu hình ZALO_GATEWAY_TOKEN.'}), 503
    if not hmac.compare_digest(expected, request.headers.get('X-Gateway-Token', '')):
        return jsonify({'success': False, 'status': 'UNAUTHORIZED', 'message': 'Không được phép truy cập gateway.'}), 401
    return None


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify(
        {
            "success": driver is not None,
            "service": "zalo-gateway",
            "status": "healthy" if driver is not None else "driver_failed",
            "dedupe_seconds": DEDUPE_SECONDS,
            "selenium_serialized": True,
        }
    )


@app.route("/gui-tin-zalo", methods=["POST"])
def gui_tin():
    data = request.get_json(silent=True) or {}

    phone = data.get("so_dien_thoai") or data.get("phone")
    message = data.get("noi_dung") or data.get("message")
    request_context = extract_request_context(data, message or "")
    zalo_name = request_context["zalo_name"]
    student_name = request_context["student_name"]
    class_name = request_context["class_name"]
    student_id = request_context["student_id"]
    tuition_amount = request_context.get("tuition_amount", "")
    tuition_month = request_context.get("tuition_month", "")
    recipient_type = request_context.get("recipient_type", "parent")

    request_id = data.get("request_id") or request.headers.get("X-Request-Id")
    force_send = False  # không cho client bỏ qua biên nhận
    if request_id:
        print(f"[ZALO][REQUEST] id={request_id}")

    if not message:
        return jsonify(
            {
                "success": False,
                "message": "Thiếu noi_dung.",
                "status": "BAD_REQUEST",
            }
        ), 400

    clean_phone = normalize_vietnam_phone(phone)
    if recipient_type == 'group':
        clean_phone = ''
        if len(norm_text(zalo_name)) < 2:
            return jsonify({'success': False, 'status': 'ZALO_GROUP_NOT_CONFIGURED', 'message': 'Lớp này chưa cấu hình nhóm Zalo.'}), 400
    if not clean_phone and not norm_text(zalo_name):
        return jsonify(
            {
                "success": False,
                "message": "Cần ít nhất một SĐT hợp lệ hoặc Tên Zalo.",
                "status": "NO_RECIPIENT_KEY",
            }
        ), 400

    message = enrich_monthly_tuition_message(
        str(message), tuition_amount=tuition_amount, tuition_month=tuition_month
    )
    recipient_key = clean_phone or ("zalo:" + norm_text(zalo_name))
    dedupe_key = build_dedupe_key(recipient_key, message, request_id=request_id)

    # Chỉ giữ lock cho transaction của người nhận chính.
    # CẢNH BÁO LỖI được gửi sau khi nhả lock để không deadlock.
    with selenium_lock:
        cleanup_recent_sends()
        fingerprint = hashlib.sha256((recipient_key + '|' + norm_text(zalo_name) + '|' + message).encode('utf-8')).hexdigest()
        journal_key = str(request_id or dedupe_key)
        receipt = _journal_claim(journal_key, fingerprint)
        if receipt is not None:
            return jsonify(receipt), 200 if receipt.get('success') else 409

        if not force_send and dedupe_key in _recent_sends:
            age = round(time.time() - _recent_sends[dedupe_key], 1)
            return jsonify(
                {
                    "success": True,
                    "message": "Yêu cầu trùng đã được chặn; không gửi lại.",
                    "status": "DUPLICATE_SKIPPED",
                    "duplicate": True,
                    "phone": clean_phone,
                    "age_seconds": age,
                }
            ), 200

        print(
            f"\n[ZALO] Đang xử lý gửi tin cho: {clean_phone or zalo_name}"
            + (f" | HS: {student_name}" if student_name else "")
            + (f" | Lớp: {class_name}" if class_name else "")
            + (f" | Zalo: {zalo_name}" if zalo_name else "")
            + (f" | Học phí: {_format_vnd_amount(tuition_amount)}" if tuition_amount else "")
        )

        result = send_message_to_phone(
            clean_phone or "",
            message,
            zalo_name=zalo_name,
            notify_on_failure=False,
            reason_context={
                "student_id": student_id,
                "student_name": student_name,
                "class_name": class_name,
                "recipient_type": recipient_type,
                "zalo_name": zalo_name,
            },
        )

        _journal_finish(journal_key, result)
        if result.get("success"):
            _recent_sends[dedupe_key] = time.time()

    # Main send thất bại -> gửi cảnh báo SAU khi lock đã được release.
    if not result.get("success"):
        result["failure_notice"] = send_failure_notice(
            clean_phone or "[không có SĐT]",
            reason=result.get("status", "SEND_FAILED"),
            original_message=message,
            reason_context={
                "student_id": student_id,
                "student_name": student_name,
                "class_name": class_name,
                "recipient_type": recipient_type,
                "zalo_name": zalo_name,
            },
            student_name=student_name,
            class_name=class_name,
            student_id=student_id,
            recipient_type=recipient_type,
            zalo_name=zalo_name,
        )
        # Ghi context đã nhận để Apps Script lưu vào gateway_response, tiện kiểm tra.
        result["received_context"] = {
            "student_id": student_id,
            "student_name": student_name,
            "class_name": class_name,
            "zalo_name": zalo_name,
            "tuition_amount": tuition_amount,
            "tuition_month": tuition_month,
            "recipient_type": recipient_type,
        }

    result["request_id"] = request_id or ""
    if result.get("success"):
        http_status = 200
    elif result.get("status") in {
        "NOT_FOUND",
        "RECIPIENT_NOT_CONFIRMED",
        "RECIPIENT_CHANGED",
        "CHAT_INPUT_NOT_FOUND",
        "COMPOSER_MISMATCH",
        "SEND_TRIGGER_FAILED",
        "AMBIGUOUS_ZALO_NAME",
        "NO_RECIPIENT_KEY",
    }:
        http_status = 409
    else:
        http_status = 422

    return jsonify(result), http_status


# -----------------------------------------------------------------------------
# V17 - KIỂM TRA TÊN ZALO/NHÓM TRƯỚC KHI GỬI (KHÔNG GỬI TIN)
# -----------------------------------------------------------------------------
_recent_lookup_status = {}
LOOKUP_CACHE_SECONDS = int(os.getenv("ZALO_LOOKUP_CACHE_SECONDS", "600"))


def _cleanup_lookup_cache():
    now = time.time()
    for key, item in list(_recent_lookup_status.items()):
        if now - float(item.get("ts", 0)) > LOOKUP_CACHE_SECONDS:
            _recent_lookup_status.pop(key, None)


@app.route("/kiem-tra-zalo", methods=["POST"])
def kiem_tra_zalo():
    """Kiểm tra gateway có mở/xác nhận được đúng tên Zalo hay không; tuyệt đối không gửi."""
    data = request.get_json(silent=True) or {}
    zalo_name = str(data.get("zalo_name") or data.get("ten_zalo") or "").strip()
    recipient_type = str(data.get("recipient_type") or "group").strip().casefold()
    if len(norm_text(zalo_name)) < 2:
        return jsonify({"success": False, "found": False, "status": "INVALID_ZALO_NAME", "message": "Tên Zalo không hợp lệ."}), 400

    cache_key = f"{recipient_type}:{norm_text(zalo_name)}"
    _cleanup_lookup_cache()
    cached = _recent_lookup_status.get(cache_key)
    if cached and cached.get("found"):
        return jsonify({
            "success": True, "found": True, "status": "FOUND_CACHED",
            "message": "Đã tìm thấy gần đây.", "zalo_name": zalo_name
        }), 200

    if driver is None:
        return jsonify({"success": False, "found": False, "status": "BOT_OFFLINE", "message": "Bot chưa khởi động thành công."}), 503

    with selenium_lock:
        try:
            driver.switch_to.default_content()
            close_search_overlay()
            chat_box, candidate_name, search_mode, err = open_recipient_by_name(zalo_name)
            found = chat_box is not None and norm_text(candidate_name) == norm_text(zalo_name)
            if found:
                _recent_lookup_status[cache_key] = {"found": True, "ts": time.time()}
                return jsonify({
                    "success": True, "found": True, "status": "FOUND",
                    "message": "Đã tìm thấy", "zalo_name": candidate_name,
                    "search_mode": search_mode
                }), 200
            status = err or "NOT_FOUND"
            message = "Có nhiều tài khoản cùng tên Zalo" if status == "AMBIGUOUS_ZALO_NAME" else "Không tìm thấy nhóm Zalo"
            _recent_lookup_status[cache_key] = {"found": False, "ts": time.time(), "status": status}
            return jsonify({"success": True, "found": False, "status": status, "message": message, "zalo_name": zalo_name}), 200
        except Exception as exc:
            return jsonify({"success": False, "found": False, "status": "LOOKUP_ERROR", "message": str(exc)}), 500


if __name__ == "__main__":
    initialize_driver()
    app.run(port=5000, host=os.getenv('ZALO_GATEWAY_HOST', '127.0.0.1'), debug=False, threaded=True)
