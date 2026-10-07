import asyncio
import datetime
from types import SimpleNamespace

import pytest
from telethon import TelegramClient, types
from telethon.sessions import MemorySession

from app import buttons as B
from app.common import UserError
from app.db import normalize_db_url
from app.channelref import parse_channel_ref
from app.handlers.panel import extract_content
from app.handlers.replace import parse_replace_args
from app.linkswap import apply_span_replacements, classify_url, normalize_username, swap_url
from app.suggest import advance_plain, build_suggestion
from app.tgutil import (
    build_markup,
    button_url,
    classify_media,
    de_entities,
    is_callback_button,
    media_from_ref,
    pack_file_id,
    ser_entities,
    ser_markup,
    utf16_len,
)

from .fakes import cb_btn, make_msg, markup, url_btn
from .harness import App

NOW = datetime.datetime.now(datetime.timezone.utc)


def photo_msg(caption="cap", entities=None):
    photo = types.Photo(1, 2, b"ref", NOW, [types.PhotoSize("x", 100, 100, 999)], 2)
    return make_msg(5, caption, entities, media=types.MessageMediaPhoto(photo=photo))


def video_msg(gif=False):
    attrs = [types.DocumentAttributeVideo(3.0, 100, 100)]
    if gif:
        attrs.append(types.DocumentAttributeAnimated())
    doc = types.Document(9, 8, b"ref", NOW, "video/mp4", 1000, 2, attrs)
    return make_msg(6, "v", media=types.MessageMediaDocument(document=doc))


# ------------------------------------------------------------------ entities / markup
def test_entity_roundtrip_keeps_formatting_and_unknown_types():
    ents = [
        types.MessageEntityBold(0, 3),
        types.MessageEntityTextUrl(4, 2, "https://t.me/x?start=1"),
        types.MessageEntityPre(7, 3, "python"),
        types.MessageEntitySpoiler(11, 2),
        types.MessageEntityUrl(14, 5),  # auto entity: not stored
        types.MessageEntityFormattedDate(20, 4, NOW),  # unknown to us: kept byte-for-byte
    ]
    data = ser_entities(ents)
    assert [d["k"] for d in data] == ["bold", "url", "pre", "spoiler", "raw"]
    back = de_entities(data)
    assert [type(e).__name__ for e in back] == [
        "MessageEntityBold", "MessageEntityTextUrl", "MessageEntityPre", "MessageEntitySpoiler", "MessageEntityFormattedDate"
    ]
    assert back[1].url == "https://t.me/x?start=1" and back[2].language == "python"


def test_markup_roundtrip_keeps_other_bots_buttons_and_colours():
    style = types.KeyboardButtonStyle(bg_success=True)
    coloured = types.KeyboardButton(text="Go", type=types.InlineButtonTypeUrl("https://t.me/x"), style=style)
    mk = markup([coloured, cb_btn("👍 3", b"react:1")], [url_btn("Two", "https://t.me/y")])
    rows = ser_markup(mk)
    assert rows[0][0]["u"] == "https://t.me/x" and "s" in rows[0][0]
    assert rows[0][1]["raw"] and rows[1][0]["u"] == "https://t.me/y"
    rebuilt = build_markup(rows)
    b0, b1 = rebuilt.rows[0].buttons
    assert button_url(b0) == "https://t.me/x" and b0.style.bg_success
    assert is_callback_button(b1) and b1.type.data == b"react:1"


def test_utf16_len_counts_emoji_as_two():
    assert utf16_len("a😀") == 3


def test_apply_span_replacements_shifts_entities():
    ents = [types.MessageEntityBold(14, 3), types.MessageEntityItalic(0, 4), types.MessageEntityCode(5, 13)]
    text, out = apply_span_replacements("open t.me/abc now", ents, [(10, 13, "longer")])
    assert text == "open t.me/longer now"
    assert (out[0].offset, out[0].length) == (17, 3)  # after the change: moved by +3
    assert (out[1].offset, out[1].length) == (0, 4)  # before it: untouched
    assert (out[2].offset, out[2].length) == (5, 16)  # spanning it: grows


# -------------------------------------------------------------------- media handling
def test_extract_content_photo_video_gif_and_text():
    c = extract_content(photo_msg("hello", [types.MessageEntityBold(0, 5)]))
    assert c["media_kind"] == "photo" and c["text"] == "hello" and c["entities"][0]["k"] == "bold"
    assert c["media_file_id"]
    assert classify_media(video_msg()) == "video" and classify_media(video_msg(gif=True)) == "animation"
    t = extract_content(make_msg(1, "plain"))
    assert t["media_kind"] is None and t["media_file_id"] is None
    with pytest.raises(UserError):
        extract_content(make_msg(1, "   "))
    with pytest.raises(UserError):
        extract_content(photo_msg("x" * 1025))
    with pytest.raises(UserError):  # a poll can't be used in a post
        extract_content(make_msg(1, "q", media=types.MessageMediaPoll(types.Poll(1, [], types.TextWithEntities("q", []), None), types.PollResults())))


def test_link_preview_photo_is_not_mistaken_for_a_photo_post():
    page = types.WebPage(id=1, url="https://x.com", display_url="x.com", hash=0, photo=types.Photo(1, 2, b"r", NOW, [], 2))
    m = make_msg(2, "look https://x.com", media=types.MessageMediaWebPage(webpage=page))
    assert classify_media(m) is None
    assert extract_content(m)["media_kind"] is None


def test_bot_file_id_can_be_reused_when_sending():
    m = photo_msg()
    fid = pack_file_id(m)
    assert fid.startswith("tl:")

    async def go(blank):
        client = TelegramClient(MemorySession(), 1, "h")
        # what send_file / edit_message do with the media object
        _, media, _ = await client._file_to_media(media_from_ref(fid, blank_reference=blank))
        return media

    assert isinstance(asyncio.run(go(False)), types.InputMediaPhoto)
    blank = asyncio.run(go(True))
    assert blank.id.file_reference == b"" and blank.id.id == 1 and blank.id.access_hash == 2
    doc_ref = pack_file_id(video_msg())
    assert media_from_ref(doc_ref).document.id == 9


# --------------------------------------------------------------------------- parsing
def test_button_parsing_and_limits():
    assert B.parse_buttons_text("A - t.me/x | B - https://y.com/?a=1\nC — https://z.com") == [
        [{"t": "A", "u": "https://t.me/x"}, {"t": "B", "u": "https://y.com/?a=1"}],
        [{"t": "C", "u": "https://z.com"}],
    ]
    for bad in ("no link here", "- https://x.com", "A - htp://x"):
        with pytest.raises(ValueError):
            B.parse_buttons_text(bad)
    with pytest.raises(ValueError):
        B.parse_buttons_text(" | ".join(f"b{i} - https://x.com/{i}" for i in range(9)))


def test_move_button_edges():
    rows = [[{"t": "a", "u": "u"}, {"t": "b", "u": "u"}], [{"t": "c", "u": "u"}]]
    assert B.move_button(rows, 0, 0, "l")[0] == rows and B.move_button(rows, 0, 0, "u")[0] == rows
    r, nr, nc = B.move_button(rows, 1, 0, "u")  # keeps its column when it can
    assert [[b["t"] for b in x] for x in r] == [["c", "a", "b"]] and (nr, nc) == (0, 0)
    r, nr, nc = B.move_button(rows, 0, 1, "d")
    assert [[b["t"] for b in x] for x in r] == [["a"], ["c", "b"]] and (nr, nc) == (1, 1)
    r, nr, nc = B.move_button([[{"t": "a"}, {"t": "b"}]], 0, 1, "d")  # last row with company -> new row
    assert [[b["t"] for b in x] for x in r] == [["a"], ["b"]] and (nr, nc) == (1, 0)
    assert B.delete_button([[{"t": "a"}]], 0, 0) == []


def test_replace_args_and_refs():
    old, new, f = parse_replace_args("@Bharath goku --last 50 --posts --channel @ch")
    assert (old, new, f["last"], f["posts"], f["channel"]) == ("Bharath", "goku", 50, True, "@ch")
    for bad in ("@a", "@a @a", "@a @b --wat", "@a @b --last x", "@a b!d"):
        with pytest.raises(ValueError):
            parse_replace_args(bad)
    assert normalize_username("https://t.me/foo_bar/") == "foo_bar" and normalize_username("@x y") is None
    assert parse_channel_ref("@animech") == "animech" and parse_channel_ref("https://t.me/c/123456/7") == 123456
    assert parse_channel_ref("-1001234567890") == 1234567890 and parse_channel_ref("???") is None


def test_url_helpers():
    assert classify_url("https://T.me/Bharath?start=x", "bharath") == "link"
    assert classify_url("https://t.me/bharath/12", "bharath") == "post"
    assert classify_url("https://t.me/bharath_bot", "bharath") is None
    assert classify_url("https://example.com/bharath", "bharath") is None
    assert swap_url("http://telegram.me/Bharath?start=A_b-C", "bharath", "goku") == "http://telegram.me/goku?start=A_b-C"


def test_db_url_normalisation():
    url, args = normalize_db_url("postgres://u:p@ep-x-pooler.eu.neon.tech/db?sslmode=require&channel_binding=require")
    assert url.startswith("postgresql+asyncpg://") and "sslmode" not in url and "channel_binding" not in url
    assert args == {"ssl": True, "statement_cache_size": 0} and "prepared_statement_cache_size=0" in url
    assert normalize_db_url("sqlite:///data/x.db")[0] == "sqlite+aiosqlite:///data/x.db"


# ------------------------------------------------------------------------ suggestions
def test_suggestion_details():
    post = SimpleNamespace(
        id=1, text="Episodes 01 to 20 are here 👇", entities=[{"k": "bold", "o": 0, "l": 8}],
        buttons=[[{"t": "Download Episodes 01 to 20", "u": "https://x"}, {"t": "Join", "u": "https://j"}],
                 [{"t": "👍 2", "raw": "AAAA"}]],
        media_kind="photo", media_file_id="FID",
    )
    s = build_suggestion(post)
    assert s.text == "Episodes 21 to 40 are here 👇" and s.entities[0]["l"] == 8
    assert s.buttons == [[{"t": "Download Episodes 21 to 40", "u": None}, {"t": "Join", "u": "https://j"}]]
    assert s.pending == [(0, 0)] and s.media_file_id == "FID"  # reactions from another bot are dropped
    assert advance_plain("no numbers") is None
    assert build_suggestion(SimpleNamespace(id=2, text="", entities=[], buttons=[[{"t": "Join", "u": "u"}]], media_kind=None, media_file_id=None)) is None


# ------------------------------------------------------- channel registration handler
def test_add_channel_by_forward_and_rights(tmp_path):
    async def run():
        app = App(tmp_path)
        await app.start()
        ch = types.Channel(id=77, title="Fwd Channel", photo=types.ChatPhotoEmpty(), date=NOW, broadcast=True,
                           access_hash=1234, username="fwdch")
        msg = SimpleNamespace(raw_text="", entities=[], media=None, grouped_id=None, forward=SimpleNamespace(chat=ch))
        await app.text("/addchannel")
        await app.text("", message=msg)
        assert "Added" in app.out.last_text
        saved = await app.db.get_channel(77)
        assert (saved.access_hash, saved.username, saved.active) == (1234, "fwdch", True)

        # a "min" channel without access details is refused with an explanation
        mini = types.Channel(id=78, title="Min", photo=types.ChatPhotoEmpty(), date=NOW, broadcast=True, min=True)
        await app.text("/addchannel")
        await app.text("", message=SimpleNamespace(raw_text="", entities=[], media=None, grouped_id=None, forward=SimpleNamespace(chat=mini)))
        assert "access details" in app.out.last_text
        assert await app.db.get_channel(78) is None
        await app.db.close()

    asyncio.run(run())


def test_media_post_publish_uses_send_file_and_caption_edit(tmp_path):
    async def run():
        app = App(tmp_path)
        await app.start()
        await app.db.save_channel(1, 5, "C", None, 1)
        p = await app.db.create_post(channel_id=1, status="draft", source="bot", text="cap", entities=[], media_kind="photo",
                                     media_file_id=pack_file_id(photo_msg()), buttons=[[{"t": "Go", "u": "https://x.com"}]], created_by=1)
        await app.press(f"pby:{p.id}")
        kind, peer, file, kw = app.tg.sent[-1]
        assert kind == "file" and isinstance(file, types.MessageMediaPhoto) and kw["caption"] == "cap"
        # editing the caption of the live media post goes through messages.editMessage
        await app.press(f"et:{p.id}")
        await app.text("new caption")
        req = app.tg.edits[-1][2]
        assert req.message == "new caption" and len(req.reply_markup.rows[0].buttons) == 1
        # replacing the media uses Telegram's file-edit path
        await app.press(f"em:{p.id}")
        await app.text("nope")
        assert "Send a photo" in app.out.last_text
        await app.db.close()

    asyncio.run(run())
