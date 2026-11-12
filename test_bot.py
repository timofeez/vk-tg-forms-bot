import bot

PEER = 2000000001


def msg(cmid, text="", from_id=1, **extra):
    return {"conversation_message_id": cmid, "from_id": from_id, "text": text, "date": 0, **extra}


class FakeVK:
    """Отдаёт историю как VK: от новых к старым, страницами."""

    def __init__(self, messages, fail=False):
        self.messages = sorted(messages, key=lambda m: -m["conversation_message_id"])
        self.fail = fail

    def history(self, peer_id, offset=0, count=bot.PAGE_SIZE):
        if self.fail:
            raise RuntimeError("VK 5 User authorization failed")
        return {"items": self.messages[offset:offset + count]}


class FakeTG:
    def __init__(self, fail_on=None):
        self.sent = []
        self.fail_on = fail_on

    def send(self, text):
        if self.fail_on and self.fail_on in text:
            raise RuntimeError("telegram down")
        self.sent.append(text)


def make_tg(tmp_path, updates=(), blocked=()):
    store = bot.Store(tmp_path / "state.json")
    tg = bot.Telegram("token", store)
    tg.delivered = []

    def call(method, wait=0, **params):
        if method == "getUpdates":
            return [u for u in updates if u["update_id"] >= params["offset"]]
        if params["chat_id"] in blocked:
            raise bot.ChatUnavailable("blocked")
        tg.delivered.append((params["chat_id"], params["text"]))

    tg._call = call
    return tg


def update(update_id, chat_id, text):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


# --- фильтр ---

def test_form_link_matches():
    f = bot.MessageFilter()
    assert f.matches(msg(1, "Вот: https://forms.gle/abc"))
    assert f.matches(msg(1, "https://docs.google.com/forms/d/xyz/viewform"))
    assert f.matches(msg(1, "https://forms.yandex.ru/u/123/"))


def test_keywords_match_but_similar_words_do_not():
    f = bot.MessageFilter()
    assert f.matches(msg(1, "Открыта запись на сдачу лаб"))
    assert f.matches(msg(1, "ФОРМА для лабораторной"))
    assert f.matches(msg(1, "заполните форму"))
    assert not f.matches(msg(1, "Важная информация о лекции"))
    assert not f.matches(msg(1, "Формально пары не будет"))
    assert not f.matches(msg(1, "Всем привет"))


def test_sender_filter():
    f = bot.MessageFilter([42])
    assert f.matches(msg(1, "запись на сдачу", from_id=42))
    assert not f.matches(msg(1, "запись на сдачу", from_id=7))
    assert not f.matches(msg(1, "просто текст", from_id=42))


def test_link_in_attachment_forward_and_reply():
    f = bot.MessageFilter()
    link = {"type": "link", "link": {"url": "https://forms.gle/x", "title": "t"}}
    assert f.matches(msg(1, attachments=[link]))
    assert f.matches(msg(1, fwd_messages=[msg(0, "https://forms.gle/x")]))
    assert f.matches(msg(1, reply_message=msg(0, "https://forms.gle/x")))


def test_keyword_in_quote_does_not_match():
    f = bot.MessageFilter()
    assert not f.matches(msg(1, "Где бы найти", reply_message=msg(0, "запись на сдачу")))
    assert not f.matches(msg(1, "Где бы найти", fwd_messages=[msg(0, "запись на сдачу")]))


def test_sender_of_forwarded_message_is_ignored():
    f = bot.MessageFilter([42])
    assert not f.matches(msg(1, from_id=7, fwd_messages=[msg(0, "форма", from_id=42)]))


# --- опрос VK ---

def test_poll_forwards_only_new_matching_in_order():
    vk = FakeVK([msg(1, "форма старая"), msg(2, "форма раз"), msg(3, "привет"), msg(4, "форма два")])
    tg, saved = FakeTG(), []
    newest, forwarded = bot.poll_once(vk, tg, bot.MessageFilter(), PEER, 1, saved.append)
    assert (newest, forwarded) == (4, 2)
    assert saved[-1] == 4
    assert "форма раз" in tg.sent[0] and "форма два" in tg.sent[1]


def test_poll_reads_all_pages():
    messages = [msg(i, "форма") for i in range(1, 451)]
    tg = FakeTG()
    bot.poll_once(FakeVK(messages), tg, bot.MessageFilter(), PEER, 0, lambda _: None)
    assert len(tg.sent) == 450


def test_telegram_failure_resends_only_unsent():
    messages = [msg(1, "форма раз"), msg(2, "форма два")]
    saved = []
    try:
        bot.poll_once(FakeVK(messages), FakeTG("форма два"), bot.MessageFilter(), PEER, 0, saved.append)
    except RuntimeError:
        pass
    assert saved == [1]
    tg = FakeTG()
    bot.poll_once(FakeVK(messages), tg, bot.MessageFilter(), PEER, saved[-1], saved.append)
    assert len(tg.sent) == 1 and "форма два" in tg.sent[0]


# --- подписки ---

def test_start_subscribes_stop_unsubscribes_and_survives_restart(tmp_path):
    tg = make_tg(tmp_path, [update(1, 10, "/start"), update(2, 20, "/start"), update(3, 10, "/stop")])
    tg.handle_updates()
    assert tg.subscribers == {20}
    restarted = bot.Store(tmp_path / "state.json")
    assert restarted.data["subscribers"] == [20] and restarted.data["tg_offset"] == 4


def test_first_subscriber_becomes_admin(tmp_path):
    tg = make_tg(tmp_path, [update(1, 10, "/start"), update(2, 20, "/start")])
    tg.handle_updates()
    assert tg.store.data["admin"] == 10


def test_other_text_does_not_subscribe(tmp_path):
    tg = make_tg(tmp_path, [update(1, 10, "привет")])
    tg.handle_updates()
    assert tg.subscribers == set() and tg.delivered[0][0] == 10


def test_broadcast_reaches_all_and_drops_blocked(tmp_path):
    tg = make_tg(tmp_path, blocked={10})
    tg.store.data["subscribers"] = [10, 20, 30]
    tg.send("форма")
    assert tg.delivered == [(20, "форма"), (30, "форма")]
    assert tg.subscribers == {20, 30}


def test_telegram_retries_temporary_errors(monkeypatch, tmp_path):
    answers = [{"ok": False, "error_code": 502, "description": "Bad Gateway"}, {"ok": True, "result": 1}]

    class Resp:
        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    monkeypatch.setattr(bot.requests, "post", lambda *a, **k: Resp(answers.pop(0)))
    tg = bot.Telegram("token", bot.Store(tmp_path / "s.json"), retry_pause=0)
    assert tg._call("sendMessage", chat_id=1, text="x") == 1


# --- отчёты и предупреждения ---

NOON = 1762938000  # 12.11.2025 12:00 МСК
EARLY = NOON - 6 * 3600  # 06:00 МСК


def setup_tick(tmp_path, vk):
    tg = make_tg(tmp_path)
    tg.store.data.update(last_cmid=0, admin=10, subscribers=[10])
    return tg, lambda now: bot.tick(vk, tg, bot.MessageFilter(), PEER, tg.store, now, 9)


def test_daily_report_once_per_day_after_hour(tmp_path):
    tg, tick = setup_tick(tmp_path, FakeVK([msg(1, "форма")]))
    tick(EARLY)
    assert not any("Бот работает" in t for _, t in tg.delivered)
    tick(NOON)
    tick(NOON + 60)
    reports = [t for _, t in tg.delivered if "Бот работает" in t]
    assert len(reports) == 1
    assert "проверок 2, переслано 1" in reports[0]


def test_alert_after_ten_minutes_of_errors_and_recovery(tmp_path):
    vk = FakeVK([], fail=True)
    tg, tick = setup_tick(tmp_path, vk)
    tg.store.data["report_date"] = "2025-11-12"
    tick(NOON)
    tick(NOON + 5 * 60)
    assert tg.delivered == []
    tick(NOON + 11 * 60)
    tick(NOON + 12 * 60)
    alerts = [t for _, t in tg.delivered if "⚠️" in t]
    assert len(alerts) == 1 and "authorization failed" in alerts[0]
    vk.fail = False
    tick(NOON + 13 * 60)
    assert "снова работает" in tg.delivered[-1][1]


def test_format_has_sender_and_chat_link():
    text = bot.format_message(
        msg(5, "форма", from_id=42), 2000000017, [{"id": 42, "first_name": "Анна", "last_name": "Иванова"}]
    )
    assert "Анна Иванова" in text and "sel=c17" in text


def test_format_removes_duplicate_link_lines():
    link = {"type": "link", "link": {"url": "https://forms.gle/x", "title": "Запись"}}
    text = bot.format_message(msg(1, "https://forms.gle/x", attachments=[link]), 2000000017)
    assert text.count("https://forms.gle/x") == 1
