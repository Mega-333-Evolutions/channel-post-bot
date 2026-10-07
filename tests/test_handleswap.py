"""/handleswap @bro @sis: the username written in posts changes - links, hyperlinks and button links do not."""
import asyncio

import pytest
from telethon import types

from app.handleswap import compute_handle_changes, find_mention_spans, mention_regex
from app.handlers.replace import parse_replace_args
from app.replace_engine import ScanOptions, apply_channel, scan_channel, undo_batch
from app.tgutil import button_url, is_callback_button
from telethon.helpers import add_surrogate

from .dbutil import db_url_for, fresh_db
from .fakes import FakeClient, cb_btn, make_msg, markup, url_btn
from .harness import App, norm


def run(coro):
    return asyncio.run(coro)


def change(text, entities=None, mk=None):
    return compute_handle_changes(make_msg(1, text, entities, mk), "bro", "sis")


def utf16(s, start, length):
    return s.encode("utf-16-le")[start * 2 : (start + length) * 2].decode("utf-16-le")


# ------------------------------------------------------------------------------- what counts as a username
def test_only_whole_usernames_are_found():
    rx = mention_regex("bro")
    found = lambda s: [m.group(0) for m in rx.finditer(s)]  # noqa: E731
    assert found("Join @bro now") == ["@bro"]
    assert found("@bro") == ["@bro"] and found("(@BRO)!") == ["@BRO"] and found("a,@bro.") == ["@bro"]
    assert found("two: @bro @Bro") == ["@bro", "@Bro"]
    assert found("Contact:@bro") == ["@bro"]
    # parts of other things are not usernames
    assert found("@bro_bot @brother @bro2") == []
    assert found("mail me: a@bro.com") == []
    assert found("https://example.com/x?u=@bro") == []
    assert found("t.me/bro and https://t.me/bro?start=1") == []


def test_a_username_in_text_is_swapped_and_the_rest_stays():
    text = "Join @bro and @Bro_bot, mail a@bro.com, https://t.me/bro?x=@bro, see t.me/bro, (@BRO)!"
    ch, skipped = change(text)
    assert ch.n_typed == 2 and ch.n_links == 0 and ch.n_buttons == 0 and skipped == 0
    assert ch.new_text == "Join @sis and @Bro_bot, mail a@bro.com, https://t.me/bro?x=@bro, see t.me/bro, (@sis)!"
    assert change("nothing to see here")[0] is None
    assert change("only links: t.me/bro https://t.me/bro?start=a")[0] is None


def test_formatting_stays_in_place_when_the_name_gets_longer_or_shorter():
    text = "Hi @bro, welcome to @bro's place 😀 and @bro"
    ents = [
        types.MessageEntityBold(3, 4),  # exactly the first @bro
        types.MessageEntityMention(3, 4),
        types.MessageEntityItalic(9, 7),  # ", welcome"[..] runs across text after it
        types.MessageEntityCode(len(text.encode("utf-16-le")) // 2 - 4, 4),  # the last @bro
    ]
    ch, _ = compute_handle_changes(make_msg(1, text, ents), "bro", "sister")
    assert ch.new_text == "Hi @sister, welcome to @sister's place 😀 and @sister"
    bold, mention, italic, code = ch.new_entities
    assert utf16(ch.new_text, bold.offset, bold.length) == "@sister" == utf16(ch.new_text, mention.offset, mention.length)
    assert utf16(ch.new_text, code.offset, code.length) == "@sister"
    assert italic.offset == 3 + 7 + 2 and italic.length == 7  # starts after the longer name: ", welcome" begins 3+7
    # shorter name
    ch, _ = compute_handle_changes(make_msg(1, "x @brother y", [types.MessageEntityBold(0, 1)]), "brother", "bo")
    assert ch.new_text == "x @bo y" and ch.new_entities[0].offset == 0


def test_links_hyperlinks_and_emails_are_left_alone():
    text = "@bro writes: https://example.com/p?u=@bro, mail x@bro.com or see @bro"
    url_start = len("@bro writes: ")
    url = "https://example.com/p?u=@bro"
    ents = [
        types.MessageEntityUrl(url_start, len(url)),
        types.MessageEntityEmail(text.index("x@bro.com"), len("x@bro.com")),
        types.MessageEntityTextUrl(len(text) - 4, 4, "https://t.me/bro"),  # the last "@bro" is a hyperlink
    ]
    ch, skipped = compute_handle_changes(make_msg(1, text, ents), "bro", "sis")
    assert ch.n_typed == 1 and ch.new_text.startswith("@sis writes: https://example.com/p?u=@bro, mail x@bro.com")
    assert ch.new_text.endswith("or see @bro")  # the hyperlinked @bro is a hyperlink: not touched
    assert skipped == 1
    link = [e for e in ch.new_entities if isinstance(e, types.MessageEntityTextUrl)][0]
    assert link.url == "https://t.me/bro"  # and its link is exactly as it was


def test_button_names_change_but_their_links_do_not():
    mk = markup(
        [url_btn("Join @bro", "https://t.me/bro?start=1"), cb_btn("👍 @bro", b"r")],
        [url_btn("Open https://t.me/bro", "https://t.me/bro")],
    )
    ch, _ = change("hello", mk=mk)
    assert (ch.n_typed, ch.n_buttons, ch.text_changed, ch.callbacks) == (0, 1, False, True)
    rows = ch.new_markup.rows
    assert rows[0].buttons[0].text == "Join @sis" and button_url(rows[0].buttons[0]) == "https://t.me/bro?start=1"
    assert is_callback_button(rows[0].buttons[1]) and rows[0].buttons[1].text == "👍 @bro"  # another bot's button
    assert rows[1].buttons[0].text == "Open https://t.me/bro"
    # no username anywhere: nothing to do and the keyboard object is not rebuilt
    assert change("hello", mk=markup([url_btn("Go", "https://t.me/bro")]))[0] is None


def test_find_mention_spans_counts_what_it_skips():
    sur = add_surrogate("@bro and @bro")
    spans, skipped = find_mention_spans(sur, [types.MessageEntityTextUrl(9, 4, "https://x.y")], "bro")
    assert spans == [(0, 4)] and skipped == 1


def test_the_arguments():
    assert parse_replace_args("@bro @sis", handles=True)[:2] == ("bro", "sis")
    assert parse_replace_args("bro sis --channel @a --last 50", handles=True)[2] == {"channel": "@a", "last": 50, "posts": False, "typed": None}
    for bad in ("@bro", "@bro @bro", "@bro @sis --posts", "@bro @sis --no-typed", "@b!ro @sis"):
        with pytest.raises(ValueError):
            parse_replace_args(bad, handles=True)


# ------------------------------------------------------------------------------- the engine
def build():
    return {
        1: make_msg(1, "Join @bro today", [types.MessageEntityBold(5, 4)], out=True),
        2: make_msg(2, "Episodes", markup=markup([url_btn("Download @bro", "https://t.me/bro?start=A"), cb_btn("👍 3", b"r")]), out=True),
        3: make_msg(3, "link only https://t.me/bro?start=B", [types.MessageEntityUrl(10, 24)]),
        4: make_msg(4, "by @bro_bot"),
        5: make_msg(5, "@BRO wrote this", media=None),  # someone else's post
        7: types.MessageEmpty(id=7, peer_id=types.PeerChannel(1)),
    }


async def engine_setup(tmp_path, msgs):
    db = await fresh_db(db_url_for(tmp_path))
    await db.save_channel(1, 5, "Test", None, 99)
    return db, await db.get_channel(1), FakeClient(msgs, pts=8)


def test_scan_apply_and_undo(tmp_path):
    async def go():
        msgs = build()
        db, ch, client = await engine_setup(tmp_path, msgs)
        opts = ScanOptions("bro", "sis", mode="handles")
        scan = await scan_channel(client, ch, opts, can_edit_others=True)
        assert set(scan.infos) == {1, 2, 5}
        assert (scan.infos[1].typed, scan.infos[2].buttons, scan.infos[2].callbacks) == (1, 1, True)
        assert scan.foreign == 1

        await db.create_batch("hs1", "bro", "sis", 99)
        res = await apply_channel(client, db, scan, opts, "hs1", 99, edit_delay=0)
        assert (res.edited, res.failed) == (3, [])
        assert msgs[1].message == "Join @sis today" and (msgs[1].entities[0].offset, msgs[1].entities[0].length) == (5, 4)
        assert msgs[5].message == "@sis wrote this"
        rows = msgs[2].reply_markup.rows
        assert rows[0].buttons[0].text == "Download @sis" and button_url(rows[0].buttons[0]) == "https://t.me/bro?start=A"
        assert is_callback_button(rows[0].buttons[1])  # the other bot's button survives the text edit
        assert msgs[2].message == "Episodes"  # only the button name changed
        assert msgs[3].message == "link only https://t.me/bro?start=B" and msgs[4].message == "by @bro_bot"

        # My posts follows
        p1 = await db.find_by_message(1, 1)
        assert p1.text == "Join @sis today"
        p2 = await db.find_by_message(1, 2)
        assert p2.buttons[0][0] == {"t": "Download @sis", "u": "https://t.me/bro?start=A"}

        back = await undo_batch(client, db, "hs1", edit_delay=0)
        assert (back.restored, back.failed) == (3, [])
        assert msgs[1].message == "Join @bro today" and msgs[5].message == "@BRO wrote this"
        assert msgs[2].reply_markup.rows[0].buttons[0].text == "Download @bro"
        await db.close()

    run(go())


# --------------------------------------------------------------------------------- the command
async def flow_app(tmp_path, **cfg):
    app = App(tmp_path, **cfg)
    await app.start()
    await app.db.save_channel(1, 5, "Anime Channel", "animech", 1)
    app.tg.msgs = build()
    app.tg._top = 8
    return app


def test_the_whole_command_with_undo(tmp_path):
    async def go():
        app = await flow_app(tmp_path)
        await app.text("/handleswap")
        assert "Usage: /handleswap @old @new" in norm(app.out.last_text)
        await app.text("/handleswap @bro @sis --posts")
        assert "Unknown option --posts" in app.out.last_text
        await app.text("/handleswap @bro")
        assert "old and the new username" in app.out.last_text

        await app.text("/handleswap @bro @sis")
        shown = norm(app.out.last_text)
        assert "swap the username @bro → @sis" in shown and "3 post(s) to change" in shown
        assert "3 mention(s) in the text" not in shown and "2 mention(s) in the text, 1 in button names" in shown
        assert "Nothing has been changed yet" in shown and "Apply to 3 post(s)" in str(app.out.last_buttons())
        assert app.tg.msgs[1].message == "Join @bro today"  # still untouched

        await app.press(app.out.callback_data("Apply to 3"))
        done = norm(app.out.last_text)
        assert "Done - username swap @bro → @sis" in done and "edited: 3" in done
        assert app.tg.msgs[1].message == "Join @sis today" and app.tg.msgs[5].message == "@sis wrote this"
        assert app.tg.msgs[2].reply_markup.rows[0].buttons[0].text == "Download @sis"
        assert "Undo this swap" in str(app.out.last_buttons())

        await app.text("/undo")
        assert "Undo the username swap @bro → @sis" in norm(app.out.last_text) and "3 post(s)" in norm(app.out.last_text)
        await app.press(app.out.callback_data("Yes, undo"))
        assert "Restored 3 post(s)" in norm(app.out.last_text)
        assert app.tg.msgs[1].message == "Join @bro today" and app.tg.msgs[2].reply_markup.rows[0].buttons[0].text == "Download @bro"
        await app.db.close()

    run(go())


def test_nothing_to_swap_and_only_the_owner(tmp_path):
    async def go():
        app = await flow_app(tmp_path)
        await app.text("/handleswap @nobody @sis")
        assert "Nothing to change" in app.out.last_text and "Apply" not in str(app.out.last_buttons())
        app.uid = 99
        await app.text("/handleswap @bro @sis")
        assert "private" in app.out.last_text
        app.uid = 1
        await app.text("/undo")
        assert "nothing to undo" in app.out.last_text
        await app.db.close()

    run(go())


def test_it_can_be_limited_to_one_channel(tmp_path):
    async def go():
        app = await flow_app(tmp_path)
        await app.text("/handleswap @bro @sis --channel @nosuchchannel")
        assert "isn't registered" in norm(app.out.last_text)
        await app.text("/handleswap @bro @sis --channel @animech")
        assert "Apply to 3 post(s)" in str(app.out.last_buttons())
        await app.db.close()

    run(go())


def test_replace_still_works_after_the_refactor(tmp_path):
    async def go():
        app = await flow_app(tmp_path)
        app.tg.msgs[3].reply_markup = markup([url_btn("Get", "https://t.me/bro?start=1")])
        await app.text("/replace @bro @sis")
        shown = norm(app.out.last_text)
        assert "replace @bro → @sis" in shown and "Apply to" in str(app.out.last_buttons())
        await app.press(app.out.callback_data("Apply to"))
        assert "Done - @bro → @sis" in norm(app.out.last_text)
        assert button_url(app.tg.msgs[3].reply_markup.rows[0].buttons[0]) == "https://t.me/sis?start=1"
        assert app.tg.msgs[1].message == "Join @bro today"  # /replace never touches a plain username
        await app.text("/undo")
        assert "Undo the replace @bro → @sis" in norm(app.out.last_text)
        await app.db.close()

    run(go())
