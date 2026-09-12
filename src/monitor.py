import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


MUBASHER_URL = "https://www.mubasher.info/news/eg/pulse/stocks"
STATE_FILE = Path("sent_news.json")

REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
MAX_ARTICLES_PER_SCAN = 100

# Telegram message limit is 4096 characters. Keep a safety margin.
TELEGRAM_CHUNK_LIMIT = 3900

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/153.0.0.0 Safari/537.36"
)

SUBSCRIPTION_KEYWORDS = [
    "اكتتاب",
    "الاكتتاب",
    "حق اكتتاب",
    "حقوق اكتتاب",
    "حق الاكتتاب",
    "حقوق الاكتتاب",
    "تداول حق الاكتتاب",
    "تداول حقوق الاكتتاب",
    "تداول الحق",
    "وتداول الحق",
    "تداول حقوق",
]

CASH_INCREASE_KEYWORDS = [
    "نقدي",
    "نقدى",
    "نقديه",
    "نقدية",
    "الاكتتاب النقدي",
    "الاكتتاب النقدى",
    "اكتتاب نقدي",
    "اكتتاب نقدى",
    "تداول الحق",
    "وتداول الحق",
    "تداول حقوق الاكتتاب",
    "تداول حق الاكتتاب",
    "حق الاكتتاب",
    "حقوق الاكتتاب",
    "حقوق الزيادة",
    "حق الزيادة",
    "زيادة نقدية",
    "زيادة نقديه",
    "زيادة نقدي",
    "زيادة نقدى",
    "سداد نقدي",
    "سداد نقدى",
    "القيمة الاسمية للاكتتاب",
]

CAPITAL_INCREASE_PHRASES = [
    "زيادة رأس المال",
    "زياده راس المال",
    "زيادة راس المال",
    "زياده رأس المال",
    "زيادة رأسمال",
    "زيادة راسمال",
    "رفع رأس المال",
    "رفع راس المال",
    "أسهم زيادة رأس المال",
    "اسهم زيادة رأس المال",
]


def log(message: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] {message}", flush=True)


def normalize_arabic(text: str) -> str:
    text = text or ""
    text = re.sub(r"[\u064B-\u065F\u0670\u06D6-\u06ED]", "", text)
    text = text.replace("ـ", "")
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي")
    text = text.replace("ة", "ه")
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def contains_any(text: str, keywords: list[str]) -> list[str]:
    normalized = normalize_arabic(text)
    return [kw for kw in keywords if normalize_arabic(kw) in normalized]


def request_with_retry(session: requests.Session, url: str) -> requests.Response:
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(
                url,
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
                allow_redirects=True,
            )
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            log(f"HTTP attempt {attempt}/{MAX_RETRIES} failed: {url} -> {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def load_state() -> set[str]:
    if not STATE_FILE.exists():
        return set()

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return set(str(x) for x in data)
        if isinstance(data, dict) and isinstance(data.get("sent"), list):
            return set(str(x) for x in data["sent"])
    except Exception as exc:
        log(f"WARNING: Could not read {STATE_FILE}: {exc}")

    return set()


def save_state(sent: set[str]) -> None:
    # Keep the repository state bounded.
    ordered = sorted(sent)[-5000:]
    STATE_FILE.write_text(
        json.dumps({"sent": ordered}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "")
    return text.strip()


def is_mubasher_news_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme in {"http", "https"}
        and "mubasher.info" in parsed.netloc
        and "/news/" in parsed.path
    )


def extract_news_from_list(html_text: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    results = []
    seen = set()

    # Mubasher's page may change CSS classes, so use URL + anchor heuristics.
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href:
            continue

        url = urljoin(MUBASHER_URL, href)
        if not is_mubasher_news_url(url):
            continue

        title = clean_text(a.get_text(" ", strip=True))
        if len(title) < 8:
            continue

        # Skip obvious navigation/section links.
        normalized_title = normalize_arabic(title)
        if normalized_title in {"اخبار", "الاخبار", "المزيد", "اقرا المزيد"}:
            continue

        # Prefer canonical URL without fragments.
        url = url.split("#", 1)[0]

        if url in seen:
            continue
        seen.add(url)

        results.append({"url": url, "title": title})

        if len(results) >= MAX_ARTICLES_PER_SCAN:
            break

    return results


def extract_article(article_html: str, url: str, fallback_title: str) -> dict:
    soup = BeautifulSoup(article_html, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        tag.decompose()

    title = ""
    for selector in ["h1", "meta[property='og:title']", "title"]:
        node = soup.select_one(selector)
        if node:
            if node.name == "meta":
                title = clean_text(node.get("content", ""))
            else:
                title = clean_text(node.get_text(" ", strip=True))
            if title:
                break

    if not title:
        title = fallback_title

    candidates = []
    selectors = [
        "article",
        "[class*='article-body']",
        "[class*='articleBody']",
        "[class*='article-content']",
        "[class*='articleContent']",
        "[class*='news-body']",
        "[class*='newsBody']",
        "[class*='news-content']",
        "[class*='newsContent']",
        "[class*='content-body']",
        "[class*='contentBody']",
        "main",
    ]

    for selector in selectors:
        for node in soup.select(selector):
            text = clean_text(node.get_text(" ", strip=True))
            if len(text) > 150:
                candidates.append(text)

    if candidates:
        body = max(candidates, key=len)
    else:
        paragraphs = [
            clean_text(p.get_text(" ", strip=True))
            for p in soup.find_all(["p", "div"])
        ]
        paragraphs = [p for p in paragraphs if len(p) >= 30]
        body = " ".join(dict.fromkeys(paragraphs))

    # Remove obvious page-title duplication at the beginning.
    if title and body.startswith(title):
        body = body[len(title):].strip(" -:|")

    return {
        "url": url,
        "title": title,
        "text": body,
    }


def decide_article(title: str, full_text: str) -> tuple[bool, str]:
    combined = f"{title}\n{full_text}"

    direct_hits = contains_any(combined, SUBSCRIPTION_KEYWORDS)
    if direct_hits:
        return True, "اكتتاب / حق اكتتاب"

    capital_hits = contains_any(combined, CAPITAL_INCREASE_PHRASES)
    if capital_hits:
        # IMPORTANT:
        # "مجاني/مجانية" is NOT an exclusion. Positive cash evidence controls.
        cash_hits = contains_any(full_text, CASH_INCREASE_KEYWORDS)
        if cash_hits:
            return True, "زيادة رأس المال + دليل على اكتتاب/زيادة نقدية"
        return False, "زيادة رأس المال بدون دليل نقدي/حق اكتتاب"

    return False, "لا توجد كلمات مطابقة"


def chunk_text(text: str, limit: int = TELEGRAM_CHUNK_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks = []
    remaining = text.strip()

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        cut = remaining.rfind("\n", 0, limit)
        if cut < int(limit * 0.5):
            cut = remaining.rfind(" ", 0, limit)
        if cut < int(limit * 0.5):
            cut = limit

        chunk = remaining[:cut].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].strip()

    return chunks


def telegram_request(
    session: requests.Session,
    token: str,
    method: str,
    payload: dict,
) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.post(url, data=payload, timeout=REQUEST_TIMEOUT)
            data = response.json()

            if response.ok and data.get("ok") is True:
                return data

            error = data.get("description", response.text[:500])
            raise RuntimeError(f"Telegram API {response.status_code}: {error}")

        except Exception as exc:
            last_error = exc
            log(f"Telegram {method} attempt {attempt}/{MAX_RETRIES} failed: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** (attempt - 1))

    raise RuntimeError(f"Telegram {method} failed after retries: {last_error}")


def send_telegram_article(
    session: requests.Session,
    token: str,
    chat_id: str,
    article: dict,
    reason: str,
) -> None:
    title = html.escape(article["title"])
    url = html.escape(article["url"], quote=True)
    full_text = article["text"].strip()

    paragraphs = [p.strip() for p in re.split(r"\n+", full_text) if p.strip()]
    summary = " ".join(paragraphs[:2])
    if len(summary) > 1200:
        summary = summary[:1197] + "..."

    header = (
        f"📰 <b>{title}</b>\n\n"
        f"🎯 <b>التصنيف:</b> {html.escape(reason)}\n\n"
        f"🔗 <a href=\"{url}\">رابط الخبر</a>\n\n"
        f"📌 <b>ملخص:</b>\n{html.escape(summary)}"
    )

    # Send header first.
    telegram_request(
        session,
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": header[:TELEGRAM_CHUNK_LIMIT],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )

    # Then send the complete article in chunks. This avoids Telegram's 4096-char limit.
    if full_text:
        chunks = chunk_text(full_text)
        for index, chunk in enumerate(chunks, start=1):
            prefix = f"📄 <b>نص الخبر ({index}/{len(chunks)})</b>\n"
            telegram_request(
                session,
                token,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": prefix + html.escape(chunk),
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )

    log(f"SENT successfully: {article['title']} | {article['url']}")


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token:
        log("ERROR: TELEGRAM_BOT_TOKEN secret is missing.")
        return 2
    if not chat_id:
        log("ERROR: TELEGRAM_CHAT_ID secret is missing.")
        return 2

    log("=" * 70)
    log("STARTING MUBASHER SCAN")
    log(f"Source: {MUBASHER_URL}")

    session = requests.Session()
    sent = load_state()
    log(f"Loaded sent state: {len(sent)} URLs")

    try:
        listing_response = request_with_retry(session, MUBASHER_URL)
    except Exception as exc:
        log(f"ERROR: Could not load Mubasher listing: {exc}")
        return 1

    news_items = extract_news_from_list(listing_response.text)
    log(f"Extracted {len(news_items)} candidate news URLs")

    if not news_items:
        log("WARNING: No news URLs found. Mubasher HTML may have changed.")
        return 0

    new_candidates = 0
    matched = 0
    sent_count = 0
    ignored = 0
    failed_articles = 0

    # IMPORTANT: open every new candidate first, then decide from the FULL article.
    # This prevents missing articles where the relevant phrase exists only in the body.
    for item in news_items:
        url = item["url"]

        if url in sent:
            continue

        new_candidates += 1
        log(f"NEW candidate: {item['title']} | {url}")

        try:
            article_response = request_with_retry(session, url)
            article = extract_article(article_response.text, url, item["title"])

            if len(article["text"]) < 50:
                log(f"WARNING: Very short article body ({len(article['text'])} chars): {url}")

            should_send, reason = decide_article(article["title"], article["text"])

            if not should_send:
                ignored += 1
                log(f"IGNORED: {article['title']} | reason={reason}")
                # We do not save ignored URLs. If Mubasher changes the article later,
                # it can be evaluated again.
                continue

            matched += 1
            log(f"MATCH: {article['title']} | reason={reason}")

            try:
                send_telegram_article(session, token, chat_id, article, reason)
            except Exception as exc:
                # DO NOT mark as sent if any Telegram message failed.
                log(f"ERROR: Telegram send failed; will retry next run: {url} -> {exc}")
                continue

            sent.add(url)
            sent_count += 1

        except Exception as exc:
            failed_articles += 1
            log(f"ERROR: Failed to process article: {url} -> {exc}")
            continue

    save_state(sent)

    log("-" * 70)
    log(
        "SCAN SUMMARY | "
        f"candidates={len(news_items)}, "
        f"new={new_candidates}, "
        f"matched={matched}, "
        f"sent={sent_count}, "
        f"ignored={ignored}, "
        f"failed={failed_articles}, "
        f"state={len(sent)}"
    )
    log("SCAN FINISHED")
    log("=" * 70)

    # The monitor intentionally exits 0 after handling individual errors.
    # This keeps the 5-minute scheduler alive while the logs show the exact failure.
    return 0


if __name__ == "__main__":
    sys.exit(main())
