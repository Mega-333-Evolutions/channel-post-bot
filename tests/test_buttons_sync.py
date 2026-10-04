"""Checking and repairing the buttons a channel really shows."""
import asyncio

import pytest

from app.buttons_sync import ButtonsNotShown, ensure_markup, fix_buttons, link_urls, scan_buttons
from app.tgutil import build_markup, button_url, peer_of

from .dbutil import db_url_for, fresh_db
from .fakes import FakeChannelClient, make_msg, markup, rpc, url_btn
from .harness import App, norm

A = "https://t.me/goku?start=A"
B = "https://t.me/goku?start=B"
ROWS = [[{"t": "Get", "u": A}], [{"t": "More", "u": B}]]


def shown(chan, i):
    m = chan.msgs[i]
    return [button_url(b) for r in (m.reply_markup.rows if m.reply_markup else []) for b in r.buttons]


class Peer:
    id, access_hash = 1, 5


def run(coro):
    return asyncio.run(coro)


def test_link_urls():
    assert link_urls(None) == []
    assert link_urls(markup([url_btn("x", A), url_btn("y", B)])) == [A, B]


def test_ensure_markup_in_every_situation():
    async def go():
        want = build_markup(ROWS)
        peer = peer_of(Peer)

        chan = FakeChannelClient({1: make_msg(1, "ok", markup=want)})
        assert await ensure_markup(chan, peer, 1, want, pause=0) == "fine"
        assert chan.edits == []

        chan = FakeChannelClient({1: make_msg(1, "no keyboard")})  # simply missing: one edit is enough
        assert await ensure_markup(chan, peer, 1, want, pause=0) == "set"
        assert shown(chan, 1) == [A, B] and len(chan.edits) == 1 and chan.msgs[1].message == "no keyboard"

        chan = FakeChannelClient({1: make_msg(1, "hidden")})  # Telegram believes the keyboard is there already
        chan.msgs[1]._hidden = want
        assert await ensure_markup(chan, peer, 1, want, pause=0) == "nudged"
        assert shown(chan, 1) == [A, B] and len(chan.edits) == 2

        chan = FakeChannelClient({})
        with pytest.raises(LookupError):
            await ensure_markup(chan, peer, 1, want, pause=0)

        chan = FakeChannelClient({1: make_msg(1, "x")})
        chan.drop_all_markup = True  # edits are accepted but the keyboard never appears
        with pytest.raises(ButtonsNotShown):
            await ensure_markup(chan, peer, 1, want, pause=0)

        chan = FakeChannelClient({1: make_msg(1, "x")})  # nothing to show -> nothing to do
        assert await ensure_markup(chan, peer, 1, None, pause=0) == "fine" and chan.requests == []

    run(go())


async def seeded(tmp_path):
    """Channel + database as /repost used to leave them: the database knows the buttons, the channel may not show them."""
    db = await fresh_db(db_url_for(tmp_path))
    await db.save_channel(1, 5, "Test", "testch", 99)
    ch = await db.get_channel(1)
    want = build_markup(ROWS)
    msgs = {
        10: make_msg(10, "fine", markup=want),
        11: make_msg(11, "missing"),
        12: make_msg(12, "hidden"),
        13: make_msg(13, "other link", markup=markup([url_btn("Get", "https://t.me/other?start=Z")])),
        15: make_msg(15, "no buttons saved"),
    }
    chan = FakeChannelClient(msgs)
    chan.msgs[12]._hidden = want
    for i in (10, 11, 12, 13, 14):  # 14 was deleted by hand
        await db.create_post(channel_id=1, message_id=i, status="sent", source="bot", text="t", entities=[], buttons=ROWS)
    await db.create_post(channel_id=1, message_id=15, status="sent", source="bot", text="t", entities=[], buttons=[])
    await db.create_post(channel_id=1, message_id=None, status="draft", source="bot", text="draft", entities=[], buttons=ROWS)
    return db, ch, chan


def test_scan_finds_exactly_the_posts_with_wrong_buttons(tmp_path):
    async def go():
        db, ch, chan = await seeded(tmp_path)
        scan = await scan_buttons(chan, ch, await db.sent_posts(1))
        assert scan.total == 5  # 10..14 have saved link buttons; 15 and the draft do not count
        assert scan.fine == 1
        assert sorted(p.message_id for p, _, _ in scan.bad) == [11, 12, 13]
        assert [p.message_id for p in scan.gone] == [14]
        live13 = next(live for p, live, _ in scan.bad if p.message_id == 13)
        assert live13 == ["https://t.me/other?start=Z"]
        await db.close()

    run(go())


def test_fix_puts_the_saved_buttons_back(tmp_path):
    async def go():
        db, ch, chan = await seeded(tmp_path)
        scan = await scan_buttons(chan, ch, await db.sent_posts(1))
        res = await fix_buttons(chan, ch, scan.bad, delay=0, pause=0)
        assert (res.fixed, res.nudged, res.already, res.gone, res.failed, res.aborted) == (3, 1, 0, 0, [], None)
        for i in (10, 11, 12, 13):
            assert shown(chan, i) == [A, B]
        assert chan.msgs[11].message == "missing"  # texts untouched
        again = await scan_buttons(chan, ch, await db.sent_posts(1))
        assert again.bad == [] and again.fine == 4 and len(again.gone) == 1
        await db.close()

    run(go())


def test_fix_gives_up_after_several_identical_refusals(tmp_path):
    async def go():
        db = await fresh_db(db_url_for(tmp_path))
        await db.save_channel(1, 5, "Test", "testch", 99)
        ch = await db.get_channel(1)
        chan = FakeChannelClient({i: make_msg(i, "x") for i in range(1, 9)})
        chan.fail["EditMessageRequest"] = rpc("MessageIdInvalidError")
        for i in range(1, 9):
            await db.create_post(channel_id=1, message_id=i, status="sent", source="bot", text="x", entities=[], buttons=ROWS)
        scan = await scan_buttons(chan, ch, await db.sent_posts(1))
        res = await fix_buttons(chan, ch, scan.bad, delay=0, pause=0)
        assert res.fixed == 0 and len(res.failed) == 5 and res.aborted == "MessageIdInvalidError"
        await db.close()

    run(go())


# ------------------------------------------------------------------------------------------ the button in My posts
def test_check_buttons_in_my_posts(tmp_path):
    async def go():
        app = App(tmp_path)
        await app.start()
        db, ch, chan = await seeded_app(app)
        await app.press("pc:1:0")
        assert "Check buttons" in str(app.out.last_buttons())
        await app.press("pk:1")
        t = norm(app.out.last_text)
        assert "checked 5 post(s)" in t and "1 show their buttons correctly" in t and "3 are saved with buttons" in t
        assert "#11" in t and "#12" in t and "#13" in t and "1 saved post(s) no longer exist" in t
        assert "Fix 3 post(s)" in str(app.out.last_buttons())
        await app.press(app.out.callback_data("Fix 3"))
        t = norm(app.out.last_text)
        assert "put the saved buttons back on 3 post(s)" in t and "1 of them needed a second edit" in t
        for i in (10, 11, 12, 13):
            assert shown(app.tg, i) == [A, B]
        await app.press("pk:1")
        assert "Everything matches" in norm(app.out.last_text) and "Fix" not in str(app.out.last_buttons())
        await app.db.close()

    run(go())


async def seeded_app(app):
    await app.db.save_channel(1, 5, "Anime Channel", "animech", 1)
    want = build_markup(ROWS)
    app.tg.msgs.update(
        {
            10: make_msg(10, "fine", markup=want),
            11: make_msg(11, "missing"),
            12: make_msg(12, "hidden"),
            13: make_msg(13, "other link", markup=markup([url_btn("Get", "https://t.me/other?start=Z")])),
            15: make_msg(15, "no buttons saved"),
        }
    )
    app.tg.msgs[12]._hidden = want
    for i in (10, 11, 12, 13, 14):
        await app.db.create_post(channel_id=1, message_id=i, status="sent", source="bot", text="t", entities=[], buttons=ROWS)
    return app.db, None, app.tg


def test_check_buttons_is_for_the_owner_only(tmp_path):
    async def go():
        app = App(tmp_path, owner=1, admins=frozenset({2}))
        await app.start()
        await seeded_app(app)
        app.uid = 2  # an admin who may make posts but is not the owner
        await app.press("pc:1:0")
        assert "Check buttons" not in str(app.out.last_buttons())
        await app.press("pk:1")
        assert "Only the bot owner" in app.out.last_text
        await app.press("pkf:1")
        assert "Only the bot owner" in app.out.last_text
        await app.db.close()

    run(go())
