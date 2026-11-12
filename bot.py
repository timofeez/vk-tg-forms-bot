"""Пересылает сообщения из беседы VK в Telegram по фильтрам.

    python bot.py                 основной цикл
    python bot.py --chats         показать беседы VK и их peer_id
    python bot.py --check 200     показать, что из последних N сообщений прошло бы фильтр

В Telegram: /start подписаться, /stop отписаться.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
VK_API = "https://api.vk.com/method/"
VK_VERSION = "5.199"
TG_LIMIT = 4096
PAGE_SIZE = 200
MSK = 3 * 3600  # Москва, UTC+3 круглый год
ALERT_AFTER = 10 * 60  # предупреждать, если ошибки идут дольше 10 минут

DEFAULT_LINK_REGEX = r"forms\.gle/|docs\.google\.com/forms|forms\.yandex\.\w+|forms\.office\.com"
DEFAULT_KEYWORD_REGEX = r"\bформ(?:а|у|е|ы|ой|ах|ам|ами)?\b|\bзапис\w*|\bсда[чт]\w*"


def load_env(path):
    """Читает KEY=VALUE из файла; уже заданные переменные окружения важнее."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def require_env(name):
    value = os.environ.get(name, "")
    if not value:
        sys.exit(f"Не задано {name} (см. README.md)")
    return value


class Store:
    """Всё состояние бота в одном JSON-файле; пишется атомарно."""

    DEFAULTS = {
        "last_cmid": None,
        "subscribers": [],
        "admin": None,
        "tg_offset": 0,
        "report_date": None,
        "checks": 0,
        "forwarded": 0,
        "errors": 0,
        "error_since": None,
        "alerted": False,
    }

    def __init__(self, path=STATE_FILE):
        self.path = path
        self.data = dict(self.DEFAULTS)
        if path.exists():
            self.data.update(json.loads(path.read_text(encoding="utf-8")))

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)


class VK:
    def __init__(self, token):
        self.token = token

    def call(self, method, **params):
        params.update(access_token=self.token, v=VK_VERSION)
        data = requests.post(VK_API + method, data=params, timeout=30).json()
        if "error" in data:
            err = data["error"]
            raise RuntimeError(f"VK {method}: {err.get('error_code')} {err.get('error_msg')}")
        return data["response"]

    def history(self, peer_id, offset=0, count=PAGE_SIZE):
        return self.call(
            "messages.getHistory", peer_id=peer_id, offset=offset, count=count, extended=1
        )


class ChatUnavailable(RuntimeError):
    """Пользователь заблокировал бота или чат больше не существует."""


class Telegram:
    def __init__(self, token, store, proxy="", retries=3, retry_pause=5):
        self.token = token
        self.store = store
        self.proxies = {"https": proxy} if proxy else None
        self.retries = retries
        self.retry_pause = retry_pause

    @property
    def subscribers(self):
        return set(self.store.data["subscribers"])

    def _set_subscribers(self, chats):
        self.store.data["subscribers"] = sorted(chats)
        self.store.save()

    def _call(self, method, wait=0, **params):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        for attempt in range(self.retries):
            last = attempt == self.retries - 1
            try:
                resp = requests.post(url, json=params, timeout=30 + wait, proxies=self.proxies)
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                # В тексте исключения requests есть URL, а в нём токен бота.
                if last:
                    raise RuntimeError(f"Telegram {method}: нет связи ({type(exc).__name__})") from None
                time.sleep(self.retry_pause)
                continue
            if data.get("ok"):
                return data["result"]
            text = f"Telegram {method}: {data.get('description')}"
            code = data.get("error_code") or 0
            if code == 403 or "chat not found" in str(data.get("description")):
                raise ChatUnavailable(text)
            if (code == 429 or code >= 500) and not last:
                time.sleep(data.get("parameters", {}).get("retry_after", self.retry_pause))
                continue
            raise RuntimeError(text)

    def send_to(self, chat_id, text):
        self._call("sendMessage", chat_id=chat_id, text=text[:TG_LIMIT])

    def send(self, text):
        """Рассылает всем подписчикам; недоступные чаты удаляет из списка."""
        for chat_id in sorted(self.subscribers):
            try:
                self.send_to(chat_id, text)
            except ChatUnavailable:
                self._set_subscribers(self.subscribers - {chat_id})

    def handle_updates(self, wait=0):
        """Обрабатывает команды; ждёт новые сообщения до wait секунд."""
        updates = self._call("getUpdates", wait=wait, offset=self.store.data["tg_offset"], timeout=wait)
        for update in updates:
            self.store.data["tg_offset"] = update["update_id"] + 1
            message = update.get("message", {})
            chat_id = message.get("chat", {}).get("id")
            command = message.get("text", "").split("@")[0].split(" ")[0]
            if chat_id is not None:
                if command == "/start":
                    self._set_subscribers(self.subscribers | {chat_id})
                    if self.store.data["admin"] is None:
                        # Первый подписчик (владелец) получает отчёты и предупреждения.
                        self.store.data["admin"] = chat_id
                    reply = "Вы подписаны на записи на сдачу лаб. Отписаться: /stop"
                elif command == "/stop":
                    self._set_subscribers(self.subscribers - {chat_id})
                    reply = "Вы отписаны. Подписаться снова: /start"
                else:
                    reply = "Подписаться: /start. Отписаться: /stop"
                try:
                    self.send_to(chat_id, reply)
                except ChatUnavailable:
                    self._set_subscribers(self.subscribers - {chat_id})
            self.store.save()


class MessageFilter:
    """Сообщение проходит, если автор разрешён И есть ссылка на форму или ключевое слово."""

    def __init__(self, sender_ids=(), link_regex=DEFAULT_LINK_REGEX, keyword_regex=DEFAULT_KEYWORD_REGEX):
        self.sender_ids = set(sender_ids)
        self.link_re = re.compile(link_regex, re.I)
        self.keyword_re = re.compile(keyword_regex, re.I)

    def matches(self, message):
        if self.sender_ids and message.get("from_id") not in self.sender_ids:
            return False
        # Ключевые слова ищем только в своём тексте: в цитатах они дают ложные срабатывания.
        return bool(
            self.link_re.search(collect_text(message))
            or self.keyword_re.search(own_text(message))
        )


def own_text(message):
    """Текст самого сообщения и его вложенных ссылок, без цитат."""
    parts = [message.get("text", "")]
    for attachment in message.get("attachments", []):
        if attachment.get("type") == "link":
            link = attachment.get("link", {})
            parts += [link.get("url", ""), link.get("title", "")]
    return "\n".join(p for p in parts if p)


def collect_text(message):
    """Текст сообщения вместе с пересланными сообщениями и ответом."""
    parts = [own_text(message)]
    for nested in message.get("fwd_messages", []):
        parts.append(collect_text(nested))
    if message.get("reply_message"):
        parts.append(collect_text(message["reply_message"]))
    return "\n".join(p for p in parts if p)


def sender_name(from_id, profiles, groups):
    for profile in profiles:
        if profile.get("id") == from_id:
            return f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
    for group in groups:
        if group.get("id") == -from_id:
            return group.get("name", str(from_id))
    return str(from_id)


def format_message(message, peer_id, profiles=(), groups=()):
    name = sender_name(message.get("from_id"), profiles, groups)
    when = time.strftime("%d.%m %H:%M", time.gmtime(message.get("date", 0) + MSK))
    open_url = f"https://vk.com/im?sel=c{peer_id - 2000000000}"
    # Ссылка часто есть и в тексте, и во вложении: повторы строк убираем.
    body = "\n".join(dict.fromkeys(collect_text(message).splitlines()))
    return f"🔔 {name}, {when}\n\n{body}\n\nОткрыть беседу: {open_url}"


def fetch_new(vk, peer_id, last_cmid):
    """Сообщения новее last_cmid от старых к новым, плюс авторы и самый новый cmid."""
    new, profiles, groups = [], [], []
    newest = last_cmid
    offset = 0
    while True:
        page = vk.history(peer_id, offset=offset)
        items = page.get("items", [])
        profiles += page.get("profiles", [])
        groups += page.get("groups", [])
        reached_seen = False
        for message in items:
            cmid = message["conversation_message_id"]
            if cmid <= last_cmid:
                reached_seen = True
                break
            new.append(message)
            newest = max(newest, cmid)
        if reached_seen or len(items) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    new.reverse()
    return new, profiles, groups, newest


def poll_once(vk, tg, message_filter, peer_id, last_cmid, save):
    """Один проход. Позиция сохраняется после каждой пересылки, так что сбой
    Telegram не теряет сообщения и не отправляет их дважды. Возвращает
    новую позицию и число пересланных сообщений."""
    new, profiles, groups, newest = fetch_new(vk, peer_id, last_cmid)
    forwarded = 0
    for message in new:
        if message_filter.matches(message):
            tg.send(format_message(message, peer_id, profiles, groups))
            print(f"Переслано сообщение {message['conversation_message_id']}", flush=True)
            save(message["conversation_message_id"])
            forwarded += 1
    if newest > last_cmid:
        save(newest)
    return newest, forwarded


def notify_admin(tg, store, text):
    admin = store.data["admin"]
    if admin is None:
        return
    try:
        tg.send_to(admin, text)
    except RuntimeError as exc:
        print(f"Не удалось отправить владельцу: {exc}", flush=True)


def tick(vk, tg, message_filter, peer_id, store, now, report_hour):
    """Одна проверка беседы плюс ежедневный отчёт и предупреждения о сбоях."""
    data = store.data

    def save(cmid):
        data["last_cmid"] = cmid
        store.save()

    try:
        _, forwarded = poll_once(vk, tg, message_filter, peer_id, data["last_cmid"], save)
        data["checks"] += 1
        data["forwarded"] += forwarded
        if data["alerted"]:
            notify_admin(tg, store, "✅ Бот снова работает, сообщения за время сбоя проверены.")
        data["error_since"], data["alerted"] = None, False
    except (RuntimeError, requests.RequestException, ValueError) as exc:
        print(f"{time.strftime('%H:%M:%S')} Ошибка: {exc}", flush=True)
        data["errors"] += 1
        data["error_since"] = data["error_since"] or now
        if not data["alerted"] and now - data["error_since"] >= ALERT_AFTER:
            notify_admin(tg, store, f"⚠️ Бот не может проверить беседу уже 10+ минут.\n{exc}")
            data["alerted"] = True

    local = time.gmtime(now + MSK)
    today = time.strftime("%Y-%m-%d", local)
    if data["report_date"] != today and local.tm_hour >= report_hour and data["admin"] is not None:
        notify_admin(
            tg,
            store,
            f"🟢 Бот работает.\nЗа сутки: проверок {data['checks']}, переслано {data['forwarded']}, "
            f"ошибок {data['errors']}.\nПодписчиков: {len(data['subscribers'])}.",
        )
        data.update(report_date=today, checks=0, forwarded=0, errors=0)
    store.save()


def build_filter():
    ids = [int(x) for x in os.environ.get("TEACHER_IDS", "").replace(" ", "").split(",") if x]
    return MessageFilter(
        ids,
        os.environ.get("LINK_REGEX") or DEFAULT_LINK_REGEX,
        os.environ.get("KEYWORD_REGEX") or DEFAULT_KEYWORD_REGEX,
    )


def run():
    vk = VK(require_env("VK_TOKEN"))
    peer_id = int(require_env("VK_PEER_ID"))
    store = Store()
    tg = Telegram(require_env("TG_BOT_TOKEN"), store, os.environ.get("TG_PROXY", ""))
    message_filter = build_filter()
    interval = int(os.environ.get("POLL_SECONDS") or 60)
    report_hour = int(os.environ.get("REPORT_HOUR") or 9)
    # RUN_MINUTES > 0: выйти через столько минут (для GitHub Actions); 0 = работать всегда.
    run_minutes = int(os.environ.get("RUN_MINUTES") or 0)
    deadline = time.time() + run_minutes * 60 if run_minutes else None

    if store.data["last_cmid"] is None:
        # Первый запуск: старую переписку не пересылаем, следим с текущего момента.
        items = vk.history(peer_id, count=1).get("items", [])
        store.data["last_cmid"] = items[0]["conversation_message_id"] if items else 0
        store.save()

    if not message_filter.sender_ids:
        print("TEACHER_IDS не задан: фильтр сработает на сообщения всех участников.")
    print(
        f"Слежу за беседой {peer_id} с сообщения {store.data['last_cmid']}, "
        f"подписчиков {len(store.data['subscribers'])}.",
        flush=True,
    )
    while deadline is None or time.time() < deadline:
        tick(vk, tg, message_filter, peer_id, store, time.time(), report_hour)
        try:
            # Заодно служит паузой: на /start бот отвечает сразу, а не раз в минуту.
            tg.handle_updates(wait=interval)
        except (RuntimeError, ValueError) as exc:
            print(f"{time.strftime('%H:%M:%S')} Ошибка: {exc}", flush=True)
            time.sleep(interval)


def show_chats():
    vk = VK(require_env("VK_TOKEN"))
    for item in vk.call("messages.getConversations", count=100)["items"]:
        conv = item["conversation"]
        if conv["peer"]["type"] == "chat":
            print(conv["peer"]["id"], conv.get("chat_settings", {}).get("title", ""))


def check(count):
    vk = VK(require_env("VK_TOKEN"))
    peer_id = int(require_env("VK_PEER_ID"))
    message_filter = build_filter()
    page = vk.history(peer_id, count=min(count, PAGE_SIZE))
    hits = [m for m in reversed(page["items"]) if message_filter.matches(m)]
    for message in hits:
        print(f"[ID автора: {message.get('from_id')}]")
        print(format_message(message, peer_id, page.get("profiles", []), page.get("groups", [])))
        print("-" * 40)
    print(f"Прошло фильтр: {len(hits)} из {len(page['items'])}")


def main():
    load_env(HERE / ".env")
    # Консоль Windows может не уметь печатать эмодзи и кириллицу из сообщений.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chats", action="store_true")
    parser.add_argument("--check", type=int, metavar="N")
    args = parser.parse_args()
    try:
        if args.chats:
            show_chats()
        elif args.check:
            check(args.check)
        else:
            run()
    except KeyboardInterrupt:
        pass
    except (RuntimeError, requests.RequestException) as exc:
        sys.exit(f"Ошибка: {exc}")


if __name__ == "__main__":
    main()
