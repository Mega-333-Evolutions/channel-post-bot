import asyncio
from telethon import types

from app.linkswap import compute_message_changes
from app.replace_engine import ScanOptions, apply_channel, scan_channel, undo_batch
from app.tgutil import button_url, is_callback_button

from .dbutil import fresh_db, db_url_for
from .fakes import FakeClient, cb_btn, make_msg, markup, rpc, url_btn

OLD = "https://t.me/bharath?start=BBB"


def build():
    text3 = "Hi 😀 https://t.me/bharath?start=CCC done"
    msgs = {
        1: make_msg(1, "Get it now", [types.MessageEntityTextUrl(0, 3, "https://t.me/Bharath?start=AAA")]),
        2: make_msg(
            2,
            "Episodes",
            markup=markup([url_btn("Ep 1-20", OLD), cb_btn("👍 3", b"r1")], [url_btn("Join", "https://t.me/other")]),
            out=True,
        ),
        3: make_msg(3, text3, [types.MessageEntityBold(37, 4)]),
        4: make_msg(4, "see https://t.me/bharath_bot?start=1"),
        5: make_msg(5, "https://t.me/bharath/123"),
        7: types.MessageEmpty(id=7, peer_id=types.PeerChannel(1)),
    }
    return msgs, text3


async def setup(tmp_path, msgs, pts=8):
    db = await fresh_db(db_url_for(tmp_path))
    await db.save_channel(1, 5, "Test", None, 99)
    ch = await db.get_channel(1)
    return db, ch, FakeClient(msgs, pts=pts)


def test_scan_apply_undo(tmp_path):
    async def run():
        msgs, text3 = build()
        db, ch, client = await setup(tmp_path, msgs)
        opts = ScanOptions("bharath", "goku")
        scan = await scan_channel(client, ch, opts, can_edit_others=True)
        assert set(scan.infos) == {1, 2, 3}
        assert scan.skipped_post_links == 1  # t.me/bharath/123 is a channel post, not a bot link
        assert scan.foreign == 2 and scan.infos[2].mine and scan.infos[2].callbacks

        await db.create_batch("b1", "bharath", "goku", 99)
        res = await apply_channel(client, db, scan, opts, "b1", 99, edit_delay=0)
        assert (res.edited, res.failed) == (3, [])

        # hyperlink: only the username changed
        assert msgs[1].entities[0].url == "https://t.me/goku?start=AAA"
        # buttons: URL swapped, reaction button + other link untouched, text untouched
        rows = msgs[2].reply_markup.rows
        assert button_url(rows[0].buttons[0]) == "https://t.me/goku?start=BBB"
        assert is_callback_button(rows[0].buttons[1])
        assert button_url(rows[1].buttons[0]) == "https://t.me/other"
        assert msgs[2].message == "Episodes"
        # typed link after an emoji: text swapped and the following entity moved with it
        assert msgs[3].message == "Hi 😀 https://t.me/goku?start=CCC done"
        assert (msgs[3].entities[0].offset, msgs[3].entities[0].length) == (34, 4)
        # untouched
        assert msgs[4].message == "see https://t.me/bharath_bot?start=1"
        assert msgs[5].message == "https://t.me/bharath/123"

        # adopted into the database
        p = await db.find_by_message(1, 3)
        assert p.source == "adopted" and "goku" in p.text
        assert (await db.find_by_message(1, 2)).buttons[0][0]["u"].endswith("goku?start=BBB")

        # undo restores everything
        ures = await undo_batch(client, db, "b1", edit_delay=0)
        assert ures.restored == 3 and not ures.failed
        assert msgs[1].entities[0].url == "https://t.me/Bharath?start=AAA"
        assert msgs[3].message == text3 and msgs[3].entities[0].offset == 37
        assert button_url(msgs[2].reply_markup.rows[0].buttons[0]) == OLD
        assert is_callback_button(msgs[2].reply_markup.rows[0].buttons[1])
        assert (await db.last_batch()) is None  # batch marked as undone
        await db.close()

    asyncio.run(run())


def test_only_own_posts_without_edit_right(tmp_path):
    async def run():
        msgs, _ = build()
        db, ch, client = await setup(tmp_path, msgs)
        opts = ScanOptions("bharath", "goku")
        scan = await scan_channel(client, ch, opts, can_edit_others=False)
        assert set(scan.infos) == {2} and scan.not_editable == 2
        await db.create_batch("b2", "bharath", "goku", 99)
        res = await apply_channel(client, db, scan, opts, "b2", 99, edit_delay=0)
        assert res.edited == 1 and len(client.edits) == 1
        await db.close()

    asyncio.run(run())


def test_failures_are_reported_not_fatal(tmp_path):
    async def run():
        msgs, _ = build()
        db, ch, client = await setup(tmp_path, msgs)
        client.fail[1] = rpc("MessageAuthorRequiredError")
        opts = ScanOptions("bharath", "goku")
        scan = await scan_channel(client, ch, opts)
        await db.create_batch("b3", "bharath", "goku", 99)
        res = await apply_channel(client, db, scan, opts, "b3", 99, edit_delay=0)
        assert res.edited == 2 and res.failed == [(1, "MessageAuthorRequiredError")]
        await db.close()

    asyncio.run(run())


def test_scan_without_counter_falls_back_to_probing(tmp_path):
    async def run():
        msgs, _ = build()
        db, ch, client = await setup(tmp_path, msgs, pts=None)
        scan = await scan_channel(client, ch, ScanOptions("bharath", "goku"))
        assert set(scan.infos) == {1, 2, 3}
        await db.close()

    asyncio.run(run())


def test_post_links_can_be_included():
    m = make_msg(5, "https://t.me/bharath/123")
    assert compute_message_changes(m, "bharath", "goku")[0] is None
    ch, skipped = compute_message_changes(m, "bharath", "goku", include_posts=True)
    assert ch.new_text == "https://t.me/goku/123"


def test_tg_scheme_and_hyperlink_and_text_flags():
    m = make_msg(
        6,
        "open tg://resolve?domain=bharath&start=x",
        [types.MessageEntityTextUrl(0, 4, "tg://resolve?domain=Bharath&start=y")],
    )
    ch, _ = compute_message_changes(m, "bharath", "goku")
    assert ch.new_text == "open tg://resolve?domain=goku&start=x"
    assert ch.new_entities[0].url == "tg://resolve?domain=goku&start=y"
    assert ch.n_typed == 1 and ch.n_links == 1

    ch2, _ = compute_message_changes(m, "bharath", "goku", include_typed=False)
    assert ch2.new_text == m.message and ch2.n_typed == 0
