import json
import os
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

load_dotenv()

QUEUE_URL = os.getenv("QUEUE_URL", "https://london.pasport.org.ua/solutions/e-queue")
TARGET_SERVICE = os.getenv("TARGET_SERVICE", "Закордонний паспорт та (або) ID-картка").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "15"))
HEADLESS = os.getenv("HEADLESS", "true").lower() not in {"0", "false", "no"}
RUN_ONCE = os.getenv("RUN_ONCE", "false").lower() in {"1", "true", "yes"}
SEND_STARTUP_MESSAGE = os.getenv("SEND_STARTUP_MESSAGE", "false").lower() in {"1", "true", "yes"}
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

PLACEHOLDER_WORDS = (
    "обрати", "оберіть", "виберіть", "select", "choose", "--", "послуга", "день", "час"
)


def now_text() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def redact(text: str) -> str:
    if BOT_TOKEN:
        return text.replace(BOT_TOKEN, "<TELEGRAM_TOKEN>")
    return text


def telegram_api(method: str, payload=None, timeout=20):
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        response = requests.post(url, json=payload or {}, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        raise RuntimeError(redact(str(exc))) from exc
    if not data.get("ok"):
        raise RuntimeError("Telegram API returned an error")
    return data


def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_state(state: dict):
    # Keep this file small and stable: GitHub commits it only when availability changes.
    stable = {
        "chat_id": state.get("chat_id") or "",
        "service": state.get("service") or TARGET_SERVICE,
        "slot_keys": sorted(set(state.get("slot_keys", []))),
        "suspended": bool(state.get("suspended", False)),
    }
    STATE_FILE.write_text(json.dumps(stable, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def discover_chat_id() -> str:
    global CHAT_ID
    if CHAT_ID:
        return CHAT_ID

    saved = load_state().get("chat_id")
    if saved:
        CHAT_ID = str(saved)
        return CHAT_ID

    data = telegram_api("getUpdates", {"timeout": 0, "limit": 100})
    for update in reversed(data.get("result", [])):
        message = update.get("message") or update.get("edited_message")
        chat = (message or {}).get("chat") or {}
        if chat.get("id") is not None:
            CHAT_ID = str(chat["id"])
            state = load_state()
            state["chat_id"] = CHAT_ID
            save_state(state)
            return CHAT_ID

    raise RuntimeError("No Telegram chat found. Open your bot and send /start first.")


def send_message(text: str):
    telegram_api(
        "sendMessage",
        {
            "chat_id": discover_chat_id(),
            "text": text,
            "disable_web_page_preview": False,
        },
    )


def clean_options(options):
    cleaned = []
    for option in options:
        text = (option.get("text") or "").strip()
        value = (option.get("value") or "").strip()
        normalized = text.casefold()
        if not text or not value or option.get("disabled"):
            continue
        if any(word in normalized for word in PLACEHOLDER_WORDS):
            continue
        cleaned.append({"text": text, "value": value})
    return cleaned


def read_options(select):
    return select.locator("option").evaluate_all(
        "els => els.map(o => ({text: o.textContent, value: o.value, disabled: o.disabled}))"
    )


def wait_for_options(select, timeout_ms=15000):
    deadline = time.time() + timeout_ms / 1000
    last = []
    while time.time() < deadline:
        last = clean_options(read_options(select))
        if last:
            return last
        time.sleep(0.5)
    return last


def choose_service(service_select, services):
    needle = TARGET_SERVICE.casefold()
    exact = [s for s in services if s["text"].casefold() == needle]
    partial = [s for s in services if needle in s["text"].casefold()]
    matches = exact or partial
    if not matches:
        names = "\n".join(f"- {s['text']}" for s in services)
        raise RuntimeError(f"Service '{TARGET_SERVICE}' was not found. Available services:\n{names}")
    selected = matches[0]
    service_select.select_option(value=selected["value"])
    return selected["text"]


def check_slots() -> dict:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="uk-UA",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        try:
            page.goto(QUEUE_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)

            body_text = page.locator("body").inner_text(timeout=10000)
            suspended = "Прийом на оформлення тимчасово призупинено" in body_text

            selects = page.locator("select")
            count = selects.count()
            if count < 3:
                raise RuntimeError(
                    f"Expected at least 3 <select> elements, found {count}. The site layout may have changed."
                )

            service_select = selects.nth(0)
            day_select = selects.nth(1)
            time_select = selects.nth(2)

            services = wait_for_options(service_select, timeout_ms=15000)
            selected_service = choose_service(service_select, services)
            page.wait_for_timeout(1200)

            days = wait_for_options(day_select, timeout_ms=7000)
            slots = []

            for day in days:
                try:
                    day_select.select_option(value=day["value"])
                    page.wait_for_timeout(800)
                    times = wait_for_options(time_select, timeout_ms=3500)
                    for t in times:
                        slots.append({"day": day["text"], "time": t["text"]})
                except PlaywrightTimeoutError:
                    continue

            return {
                "service": selected_service,
                "suspended": suspended,
                "slots": slots,
                "checked_at": now_text(),
            }
        finally:
            browser.close()


def slot_key(slot: dict) -> str:
    return f"{slot['day']}|{slot['time']}"


def notify_if_needed(result: dict):
    state = load_state()
    previous = set(state.get("slot_keys", []))
    current = {slot_key(s) for s in result["slots"]}
    new_slots = [s for s in result["slots"] if slot_key(s) not in previous]

    if new_slots:
        lines = [
            "🔔 З'ЯВИЛИСЯ ВІЛЬНІ МІСЦЯ!",
            f"Послуга: {result['service']}",
            "",
        ]
        for slot in new_slots[:20]:
            lines.append(f"📅 {slot['day']}   🕐 {slot['time']}")
        if len(new_slots) > 20:
            lines.append(f"…і ще {len(new_slots) - 20} слотів")
        lines += ["", "Відкрий сторінку та бронюй:", QUEUE_URL]
        send_message("\n".join(lines))

    state.update(
        {
            "slot_keys": sorted(current),
            "service": result["service"],
            "suspended": result["suspended"],
        }
    )
    save_state(state)


def run_check():
    result = check_slots()
    print(
        f"[{result['checked_at']}] service={result['service']!r} "
        f"slots={len(result['slots'])} suspended={result['suspended']}",
        flush=True,
    )
    notify_if_needed(result)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN")

    # Make sure /start was sent before the browser check. This also persists chat_id.
    discover_chat_id()

    if SEND_STARTUP_MESSAGE:
        send_message(
            "✅ Монітор черги запущено.\n"
            f"Послуга: {TARGET_SERVICE}\n"
            f"Сторінка: {QUEUE_URL}"
        )

    if RUN_ONCE:
        run_check()
        return

    if CHECK_INTERVAL_MINUTES < 5:
        raise SystemExit("CHECK_INTERVAL_MINUTES must be at least 5 minutes")

    while True:
        started = time.time()
        try:
            run_check()
        except Exception as exc:
            print(f"[{now_text()}] ERROR: {redact(str(exc))}", flush=True)
        elapsed = time.time() - started
        time.sleep(max(30, CHECK_INTERVAL_MINUTES * 60 - elapsed))


if __name__ == "__main__":
    main()
