import os
import re
import json
import time
import hashlib
import logging
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


# ============================================================
# CONFIG
# ============================================================

MUBASHER_URL = "https://www.mubasher.info/news/eg/pulse/stocks"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STATE_FILE = "sent_news.json"

REQUEST_TIMEOUT = 25
MAX_RETRIES = 4

MAX_ARTICLES_PER_SCAN = 100

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("mubasher-monitor")


# ============================================================
# ARABIC NORMALIZATION
# ============================================================

def normalize_arabic(text: str) -> str:
    """
    Normalize Arabic text to reduce spelling/Unicode variations.
    """

    if not text:
        return ""

    text = str(text)

    # Remove Arabic diacritics
    text = re.sub(r"[\u064B-\u065F\u0670]", "", text)

    # Normalize Alef variations
    text = re.sub(r"[إأآٱ]", "ا", text)

    # Normalize Ya / Alef Maqsura
    text = text.replace("ى", "ي")

    # Normalize Ta Marbuta
    text = text.replace("ة", "ه")

    # Tatweel
    text = text.replace("ـ", "")

    # Normalize whitespace
    text = re.sub(r"\s+", " ", text)

    return text.strip().lower()


# ============================================================
# KEYWORDS
# ============================================================

# Direct subscription / rights keywords.
SUBSCRIPTION_KEYWORDS = [
    "اكتتاب",
    "الاكتتاب",
    "حق اكتتاب",
    "حقوق الاكتتاب",
    "حق الاكتتاب",
    "حقوق اكتتاب",
    "تداول حق الاكتتاب",
    "تداول حقوق الاكتتاب",
    "تداول الحق",
    "وتداول الحق",
    "تداول حقوق",
    "الاكتتاب النقدي",
    "الاكتتاب النقدى",
    "اكتتاب نقدي",
    "اكتتاب نقدى",
]


# Terms that indicate a CASH capital increase / tradable subscription right.
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


# NOTE:
# We deliberately DO NOT use "مجاني" / "مجانية" as an exclusion rule.
#
# Example:
# "زيادة رأس المال من خلال أسهم مجانية مع زيادة نقدية للاكتتاب"
#
# must still be sent because the article contains a positive
# cash/subscription indicator.


# ============================================================
# HTTP
# ============================================================

session = requests.Session()
session.headers.update(HEADERS)


def http_get(url: str):
    """
    GET request with retries and exponential backoff.
    """

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):

        try:

            logger.info(
                "GET %s | attempt %s/%s",
                url,
                attempt,
                MAX_RETRIES,
            )

            response = session.get(
                url,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )

            response.raise_for_status()

            return response

        except Exception as exc:

            last_error = exc

            logger.warning(
                "Request failed: %s",
                exc,
            )

            if attempt < MAX_RETRIES:
                sleep_time = 2 ** (attempt - 1)
                time.sleep(sleep_time)

    raise RuntimeError(
        f"Failed to download {url}: {last_error}"
    )


# ============================================================
# STATE
# ============================================================

def load_state():
    """
    Load sent article URLs/hashes.
    """

    if not os.path.exists(STATE_FILE):
        return {
            "sent": {}
        }

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8",
        ) as f:

            data = json.load(f)

        if not isinstance(data, dict):
            return {"sent": {}}

        if "sent" not in data:
            data["sent"] = {}

        return data

    except Exception as exc:

        logger.warning(
            "Could not read state file: %s",
            exc,
        )

        return {
            "sent": {}
        }


def save_state(state):
    """
    Atomically save state.
    """

    temp_file = STATE_FILE + ".tmp"

    with open(
        temp_file,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(
        temp_file,
        STATE_FILE,
    )


def make_news_id(url: str) -> str:
    """
    Stable identifier based on canonical URL.
    """

    canonical = url.strip().rstrip("/")

    return hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()


# ============================================================
# MUBASHER LIST PAGE
# ============================================================

def extract_news_from_list(html: str):
    """
    Extract news links and titles from Mubasher stock-news page.

    We intentionally collect MANY articles instead of only the first one.
    """

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    articles = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):

        href = a.get("href", "").strip()

        title = a.get_text(
            " ",
            strip=True,
        )

        if not href or not title:
            continue

        full_url = urljoin(
            MUBASHER_URL,
            href,
        )

        # We only want Mubasher article URLs.
        if "mubasher.info" not in full_url:
            continue

        # Ignore navigation/category links.
        if "/news/" not in full_url:
            continue

        # Ignore the current listing page itself.
        if full_url.rstrip("/") == MUBASHER_URL.rstrip("/"):
            continue

        # Avoid duplicates.
        normalized_url = full_url.rstrip("/")

        if normalized_url in seen_urls:
            continue

        # Very short titles are generally navigation elements.
        if len(title) < 15:
            continue

        seen_urls.add(normalized_url)

        articles.append(
            {
                "url": normalized_url,
                "title": title,
            }
        )

        if len(articles) >= MAX_ARTICLES_PER_SCAN:
            break

    logger.info(
        "Extracted %s article links",
        len(articles),
    )

    return articles


# ============================================================
# ARTICLE PAGE
# ============================================================

def extract_article(url: str):
    """
    Open the actual Mubasher article and extract title + text.
    """

    response = http_get(url)

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    # Remove irrelevant elements.
    for element in soup(
        [
            "script",
            "style",
            "noscript",
            "svg",
            "iframe",
        ]
    ):
        element.decompose()

    title = ""

    # Try common title locations.
    og_title = soup.find(
        "meta",
        property="og:title",
    )

    if og_title and og_title.get("content"):
        title = og_title["content"].strip()

    if not title:

        h1 = soup.find("h1")

        if h1:
            title = h1.get_text(
                " ",
                strip=True,
            )

    if not title:

        if soup.title:
            title = soup.title.get_text(
                " ",
                strip=True,
            )

    # Try to identify article body.
    candidate_selectors = [
        "article",
        "[class*='article-body']",
        "[class*='articleBody']",
        "[class*='article-content']",
        "[class*='articleContent']",
        "[class*='news-content']",
        "[class*='newsContent']",
        "[class*='story-body']",
        "[class*='storyBody']",
    ]

    body = None

    for selector in candidate_selectors:

        candidate = soup.select_one(
            selector
        )

        if candidate:

            text = candidate.get_text(
                "\n",
                strip=True,
            )

            if len(text) > 150:

                body = candidate
                break

    if body is None:

        # Fallback:
        # collect meaningful paragraphs.
        paragraphs = soup.find_all("p")

        texts = []

        for p in paragraphs:

            text = p.get_text(
                " ",
                strip=True,
            )

            if len(text) >= 30:
                texts.append(text)

        article_text = "\n".join(texts)

    else:

        article_text = body.get_text(
            "\n",
            strip=True,
        )

    # Cleanup
    article_text = re.sub(
        r"\n{3,}",
        "\n\n",
        article_text,
    )

    article_text = article_text.strip()

    return {
        "title": title.strip(),
        "text": article_text,
        "url": url,
    }


# ============================================================
# MATCHING ENGINE
# ============================================================

def contains_any(text, keywords):

    normalized = normalize_arabic(text)

    for keyword in keywords:

        normalized_keyword = normalize_arabic(
            keyword
        )

        if normalized_keyword in normalized:
            return True

    return False


def find_matches(text, keywords):

    normalized = normalize_arabic(text)

    matches = []

    for keyword in keywords:

        normalized_keyword = normalize_arabic(
            keyword
        )

        if normalized_keyword in normalized:

            matches.append(keyword)

    return list(dict.fromkeys(matches))


def should_send(title: str, article_text: str):
    """
    Main decision engine.

    RULES:

    1. If article is directly about subscription / rights:
       SEND.

    2. If title/article indicates capital increase:
       inspect full article.

       SEND only if there is a positive cash/subscription-right
       indicator.

    3. "free/free shares" is NOT a negative rule.
       It cannot cancel a positive cash indicator.
    """

    title_normalized = normalize_arabic(title)
    article_normalized = normalize_arabic(article_text)

    combined = (
        title_normalized
        + "\n"
        + article_normalized
    )

    # --------------------------------------------------------
    # DIRECT SUBSCRIPTION
    # --------------------------------------------------------

    direct_matches = find_matches(
        combined,
        SUBSCRIPTION_KEYWORDS,
    )

    if direct_matches:

        return {
            "send": True,
            "reason": "اشتراك/حق اكتتاب",
            "matches": direct_matches,
        }

    # --------------------------------------------------------
    # CAPITAL INCREASE
    # --------------------------------------------------------

    capital_matches = find_matches(
        combined,
        CAPITAL_INCREASE_PHRASES,
    )

    if capital_matches:

        # IMPORTANT:
        # Only positive evidence can trigger sending.
        cash_matches = find_matches(
            article_text,
            CASH_INCREASE_KEYWORDS,
        )

        if cash_matches:

            return {
                "send": True,
                "reason": "زيادة رأس المال + مؤشر نقدي/حق اكتتاب",
                "matches": cash_matches,
            }

        return {
            "send": False,
            "reason": "زيادة رأس المال بدون مؤشر اكتتاب نقدي/تداول حق",
            "matches": [],
        }

    return {
        "send": False,
        "reason": "لا يوجد تطابق",
        "matches": [],
    }


# ============================================================
# TELEGRAM
# ============================================================

def telegram_api(method: str, payload=None):

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing"
        )

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/"
        f"{method}"
    )

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):

        try:

            response = session.post(
                url,
                json=payload or {},
                timeout=REQUEST_TIMEOUT,
            )

            response.raise_for_status()

            data = response.json()

            if not data.get("ok"):
                raise RuntimeError(
                    data
                )

            return data

        except Exception as exc:

            last_error = exc

            logger.warning(
                "Telegram error: %s",
                exc,
            )

            if attempt < MAX_RETRIES:

                time.sleep(
                    2 ** (attempt - 1)
                )

    raise RuntimeError(
        f"Telegram failed: {last_error}"
    )


def send_telegram_message(
    title,
    summary,
    full_text,
    url,
    reason,
):

    if not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID is missing"
        )

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    # Telegram messages have a length limit.
    # Keep enough of the article to remain useful.
    max_article_chars = 7000

    if len(full_text) > max_article_chars:

        full_text = (
            full_text[:max_article_chars]
            + "\n\n[تم اختصار النص بسبب طول الرسالة]"
        )

    message = (
        "🚨 <b>تنبيه: خبر اكتتاب / زيادة رأس المال</b>\n\n"
        f"📰 <b>العنوان:</b>\n"
        f"{escape_html(title)}\n\n"
        f"📌 <b>ملخص الخبر:</b>\n"
        f"{escape_html(summary)}\n\n"
        f"📄 <b>نص الخبر:</b>\n"
        f"{escape_html(full_text)}\n\n"
        f"🔎 <b>سبب التنبيه:</b>\n"
        f"{escape_html(reason)}\n\n"
        f"🔗 <b>رابط الخبر:</b>\n"
        f"{url}\n\n"
        f"⏰ <b>وقت الرصد:</b> {now}"
    )

    return telegram_api(
        "sendMessage",
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
    )


def escape_html(text):

    if not text:
        return ""

    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ============================================================
# MAIN SCAN
# ============================================================

def scan():

    logger.info("=" * 70)
    logger.info("STARTING MUBASHER SCAN")
    logger.info("=" * 70)

    state = load_state()

    sent = state.setdefault(
        "sent",
        {},
    )

    # --------------------------------------------------------
    # 1. Get listing page
    # --------------------------------------------------------

    response = http_get(
        MUBASHER_URL
    )

    articles = extract_news_from_list(
        response.text
    )

    if not articles:

        logger.warning(
            "No articles found."
        )

        return

    sent_count = 0
    matched_count = 0

    # --------------------------------------------------------
    # 2. Process ALL discovered articles
    # --------------------------------------------------------

    for index, article in enumerate(
        articles,
        start=1,
    ):

        url = article["url"]
        listing_title = article["title"]

        news_id = make_news_id(
            url
        )

        logger.info(
            "[%s/%s] %s",
            index,
            len(articles),
            listing_title,
        )

        # Already sent?
        if news_id in sent:

            logger.info(
                "Already sent -> skip"
            )

            continue

        # ----------------------------------------------------
        # 3. First cheap check:
        # title / listing text
        # ----------------------------------------------------

        title_has_relevant_word = (
            contains_any(
                listing_title,
                SUBSCRIPTION_KEYWORDS
                + CAPITAL_INCREASE_PHRASES,
            )
        )

        # If title is clearly irrelevant,
        # we normally do not need to open it.
        #
        # BUT:
        # we also allow scanning the first page's recent
        # articles more broadly if the title contains
        # related terms.

        if not title_has_relevant_word:

            continue

        # ----------------------------------------------------
        # 4. Open full article
        # ----------------------------------------------------

        try:

            article_data = extract_article(
                url
            )

        except Exception as exc:

            logger.error(
                "Failed to parse article: %s | %s",
                url,
                exc,
            )

            # Do NOT mark as sent.
            # It can be retried next scan.
            continue

        title = (
            article_data["title"]
            or listing_title
        )

        full_text = article_data[
            "text"
        ]

        # ----------------------------------------------------
        # 5. Decision engine
        # ----------------------------------------------------

        decision = should_send(
            title,
            full_text,
        )

        if not decision["send"]:

            logger.info(
                "IGNORED | %s",
                decision["reason"],
            )

            # Not sent.
            # We don't need to remember it.
            continue

        matched_count += 1

        # ----------------------------------------------------
        # 6. Build summary
        # ----------------------------------------------------

        summary = ""

        # Use first meaningful paragraphs as summary.
        paragraphs = [
            x.strip()
            for x in full_text.splitlines()
            if len(x.strip()) >= 30
        ]

        if paragraphs:

            summary = " ".join(
                paragraphs[:2]
            )

        if len(summary) > 1200:

            summary = (
                summary[:1200]
                + "..."
            )

        # ----------------------------------------------------
        # 7. Send Telegram
        # ----------------------------------------------------

        try:

            telegram_result = (
                send_telegram_message(
                    title=title,
                    summary=summary,
                    full_text=full_text,
                    url=url,
                    reason=decision[
                        "reason"
                    ],
                )
            )

            # IMPORTANT:
            # Only mark as sent AFTER Telegram
            # successfully confirms delivery.

            sent[news_id] = {
                "url": url,
                "title": title,
                "sent_at": datetime.utcnow().isoformat()
                + "Z",
                "telegram_message_id": (
                    telegram_result
                    .get("result", {})
                    .get("message_id")
                ),
            }

            save_state(
                state
            )

            sent_count += 1

            logger.info(
                "SENT successfully: %s",
                title,
            )

        except Exception as exc:

            logger.error(
                "Telegram send failed. "
                "Article will be retried next scan. "
                "%s",
                exc,
            )

            # DO NOT add to sent.
            continue

    # --------------------------------------------------------
    # 8. Cleanup old state
    # --------------------------------------------------------

    # Keep only the latest 5000 sent news records.
    # This prevents the state file from growing forever.
    if len(sent) > 5000:

        sorted_items = sorted(
            sent.items(),
            key=lambda item: item[1].get(
                "sent_at",
                "",
            ),
            reverse=True,
        )

        state["sent"] = dict(
            sorted_items[:5000]
        )

        save_state(
            state
        )

    logger.info("=" * 70)
    logger.info(
        "SCAN FINISHED | sent=%s | matched=%s | articles=%s",
        sent_count,
        matched_count,
        len(articles),
    )
    logger.info("=" * 70)


# ============================================================
# TEST MODE
# ============================================================

def main():

    try:

        scan()

    except Exception as exc:

        logger.exception(
            "FATAL SCAN ERROR: %s",
            exc,
        )

        # Exit with error so GitHub Actions
        # clearly shows that this scan failed.
        raise


if __name__ == "__main__":
    main()
