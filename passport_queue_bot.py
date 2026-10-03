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

NO_SLOTS_PHRASES = (
    "На даний момент відсутні місця в електронній черзі",
    "Наразі вільні слоти відсутні",
    "вільні місця відсутні",
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


def _clean_text_list(items):
    out = []
    for text in items:
        text = (text or "").strip()
        low = text.casefold()
        if not text or any(word in low for word in PLACEHOLDER_WORDS):
            continue
        if text not in out:
            out.append(text)
    return out


def _visible_option_texts(page):
    # Works with many custom dropdown libraries that expose ARIA option roles.
    try:
        return _clean_text_list(page.get_by_role("option").all_inner_texts())
    except Exception:
        return []


def _choose_custom_option(page, combo, text):
    combo.click()
    page.wait_for_timeout(400)
    option = page.get_by_role("option", name=text, exact=True)
    if option.count() == 0:
        # Fallback for dropdowns implemented as plain text list items/buttons.
        option = page.get_by_text(text, exact=True)
    option.first.click()


def _check_custom_combos(page, combos):
    service_combo = combos.nth(0)
    day_combo = combos.nth(1)

    # Select only the requested service.
    service_combo.click()
    page.wait_for_timeout(500)
    service_options = _visible_option_texts(page)
    target = next((x for x in service_options if x.casefold() == TARGET_SERVICE.casefold()), None)
    if not target:
        target = next((x for x in service_options if TARGET_SERVICE.casefold() in x.casefold()), None)
    if not target:
        raise RuntimeError(
            "Target service was not found in the custom service dropdown. "
            f"Visible options: {service_options[:20]}"
        )
    page.get_by_role("option", name=target, exact=True).first.click()
    page.wait_for_timeout(1200)

    # We only need available dates to know that booking has opened.
    day_combo.click()
    page.wait_for_timeout(700)
    days = _visible_option_texts(page)
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return target, [{"day": d, "time": ""} for d in days]


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
            page.wait_for_timeout(2500)

            body_text = page.locator("body").inner_text(timeout=10000)
            suspended = "Прийом на оформлення тимчасово призупинено" in body_text
            no_slots = any(phrase.casefold() in body_text.casefold() for phrase in NO_SLOTS_PHRASES)

            # When the site explicitly says there are no places, that is a normal
            # "0 slots" state, not a bot failure. A maintenance banner alone is not
            # enough to stop parsing because the form may still be present.
            if no_slots:
                return {
                    "service": TARGET_SERVICE,
                    "suspended": suspended,
                    "slots": [],
                    "checked_at": now_text(),
                }

            # Path 1: native HTML <select> controls.
            selects = page.locator("select")
            if selects.count() >= 2:
                service_select = selects.nth(0)
                day_select = selects.nth(1)

                services = wait_for_options(service_select, timeout_ms=15000)
                selected_service = choose_service(service_select, services)
                page.wait_for_timeout(1200)
                days = wait_for_options(day_select, timeout_ms=7000)

                # Available dates are enough for an immediate alert; the user opens
                # the official page and completes booking/Diia or BankID manually.
                slots = [{"day": d["text"], "time": ""} for d in days]
                return {
                    "service": selected_service,
                    "suspended": suspended,
                    "slots": slots,
                    "checked_at": now_text(),
                }

            # Path 2: modern/custom dropdown controls (ARIA comboboxes).
            combos = page.get_by_role("combobox")
            if combos.count() >= 2:
                selected_service, slots = _check_custom_combos(page, combos)
                return {
                    "service": selected_service,
                    "suspended": suspended,
                    "slots": slots,
                    "checked_at": now_text(),
                }

            # During a full maintenance closure the rendered form may disappear.
            if suspended:
                return {
                    "service": TARGET_SERVICE,
                    "suspended": True,
                    "slots": [],
                    "checked_at": now_text(),
                }

            # If the explicit "no slots" text disappeared but the form is still not
            # parseable, treat this as important: the page state changed and the user
            # should check it immediately instead of silently missing an opening.
            snippet = " ".join(body_text.split())[:700]
            raise RuntimeError(
                "Queue page changed: the no-slots message is gone, but no supported "
                f"dropdowns were found. Open the page immediately. Page text: {snippet}"
            )
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
            if slot.get("time"):
                lines.append(f"📅 {slot['day']}   🕐 {slot['time']}")
            else:
                lines.append(f"📅 {slot['day']}")
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
        try:
            run_check()
        except Exception as exc:
            try:
                send_message(
                    "⚠️ Монітор не зміг перевірити чергу.\n"
                    "Сторінка могла змінитися або тимчасово блокувати автоматичну перевірку.\n"
                    f"Помилка: {redact(str(exc))[:1200]}\n\n"
                    f"Перевірити вручну: {QUEUE_URL}"
                )
            except Exception:
                pass
            raise
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
