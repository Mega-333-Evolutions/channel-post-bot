"""Links to other posts of the same channel: found in all three places and pointed at the new message ids."""
from telethon import types
from telethon.helpers import add_surrogate

from app.postlinks import PostLinks, parse_ref, relink_message, resolve_chain
from app.tgutil import button_url

from .fakes import make_msg, markup, url_btn


def links(ids=None, username="One_Piece_inEnglish", cid=1234567):
    return PostLinks(username=username, channel_id=cid, ids=dict(ids or {}))


def utf16(text, ent):
    raw = text.encode("utf-16-le")
    return raw[ent.offset * 2 : (ent.offset + ent.length) * 2].decode("utf-16-le")


# ------------------------------------------------------------------------------------------- parsing
def test_the_forms_of_a_post_link_are_recognised():
    r = parse_ref("https://t.me/One_Piece_inEnglish/115")
    assert (r.name, r.cid, r.msg_id) == ("one_piece_inenglish", None, 115)
    assert "https://t.me/One_Piece_inEnglish/115"[r.start : r.end] == "115"
    r = parse_ref("t.me/c/1234567/56?single")
    assert (r.name, r.cid, r.msg_id) == (None, 1234567, 56)
    assert parse_ref("http://telegram.me/somechan/7#x").msg_id == 7
    assert parse_ref("https://t.me/s/somechan/7").msg_id == 7
    assert parse_ref("https://telegram.dog/somechan/7/2").msg_id == 7  # only the first number is the post
    r = parse_ref("tg://resolve?domain=somechan&post=9")
    assert (r.name, r.msg_id) == ("somechan", 9)
    r = parse_ref("tg://resolve?post=9&domain=somechan")
    assert (r.name, r.msg_id) == ("somechan", 9)
    r = parse_ref("tg://privatepost?channel=1234567&post=9")
    assert (r.cid, r.msg_id) == (1234567, 9)


def test_links_that_are_not_post_links_are_not_touched():
    for url in (
        "https://t.me/somechan",
        "https://t.me/somechan?start=ABC123",
        "https://t.me/+AbCdEfGhIj",
        "https://t.me/addstickers/pack",
        "https://example.com/somechan/12",
        "https://t.me/c/1234567",
        "tg://resolve?domain=somechan",
        "",
        None,
    ):
        assert parse_ref(url) is None, url


def test_only_links_into_this_channel_count():
    pl = links({115: 400})
    assert pl.ref_of("https://t.me/one_piece_inenglish/115")  # a username is not case sensitive
    assert pl.ref_of("https://t.me/c/1234567/115")
    assert pl.ref_of("https://t.me/c/999/115") is None
    assert pl.ref_of("https://t.me/another_channel/115") is None
    private = PostLinks(username=None, channel_id=1234567, ids={115: 400})
    assert private.ref_of("https://t.me/One_Piece_inEnglish/115") is None
    assert private.remap_url("https://t.me/c/1234567/115") == "https://t.me/c/1234567/400"


def test_only_the_digits_of_the_post_id_change():
    pl = links({115: 1042, 7: 8})
    assert pl.remap_url("https://t.me/One_Piece_inEnglish/115") == "https://t.me/One_Piece_inEnglish/1042"
    assert pl.remap_url("https://t.me/One_Piece_inEnglish/115?single") == "https://t.me/One_Piece_inEnglish/1042?single"
    assert pl.remap_url("t.me/c/1234567/7?comment=7") == "t.me/c/1234567/8?comment=7"
    assert pl.remap_url("tg://resolve?domain=One_Piece_inEnglish&post=115") == "tg://resolve?domain=One_Piece_inEnglish&post=1042"
    assert pl.remap_url("tg://resolve?post=115&domain=One_Piece_inEnglish&x=1") == "tg://resolve?post=1042&domain=One_Piece_inEnglish&x=1"
    assert pl.remap_url("tg://privatepost?channel=1234567&post=7") == "tg://privatepost?channel=1234567&post=8"
    # a post nobody copied, another channel, a bot link: unchanged
    assert pl.remap_url("https://t.me/One_Piece_inEnglish/116") == "https://t.me/One_Piece_inEnglish/116"
    assert pl.remap_url("https://t.me/other/115") == "https://t.me/other/115"
    assert pl.remap_url("https://t.me/goku?start=115") == "https://t.me/goku?start=115"


# --------------------------------------------------------------------------------------- the text
def test_typed_links_are_rewritten_and_the_formatting_after_them_moves_along():
    text = "😀 see https://t.me/One_Piece_inEnglish/9 and bold t.me/c/1234567/10?single end"
    bold = types.MessageEntityBold(text.index("bold") + 0, 4)
    bold.offset = len(text[: text.index("bold")].encode("utf-16-le")) // 2  # offsets count UTF-16 units, the emoji is 2
    url_ent = types.MessageEntityUrl(len("😀 see ".encode("utf-16-le")) // 2, len("https://t.me/One_Piece_inEnglish/9"))
    pl = links({9: 1234, 10: 12})
    new_text, ents, n_typed, n_hyper = pl.remap_text(text, [bold, url_ent])
    assert new_text == "😀 see https://t.me/One_Piece_inEnglish/1234 and bold t.me/c/1234567/12?single end"
    assert (n_typed, n_hyper) == (2, 0)
    b, u = ents
    assert utf16(new_text, b) == "bold"  # the bold span moved with its text
    assert utf16(new_text, u) == "https://t.me/One_Piece_inEnglish/1234"  # the link entity grew with the link
    assert bold.offset != b.offset  # the originals were not modified


def test_a_link_typed_inside_another_word_or_url_is_not_a_post_link():
    pl = links({9: 99})
    text = "x https://example.com/?u=t.me/One_Piece_inEnglish/9 and email@t.me/One_Piece_inEnglish/9"
    assert pl.remap_text(text, [])[0] == text


def test_hyperlinks_are_changed_without_touching_their_text():
    text = "Click here and there"
    ents = [
        types.MessageEntityTextUrl(0, 10, "https://t.me/One_Piece_inEnglish/115"),
        types.MessageEntityTextUrl(11, 3, "https://t.me/another/115"),
        types.MessageEntityBold(15, 5),
    ]
    new_text, new_ents, n_typed, n_hyper = links({115: 2000}).remap_text(text, ents)
    assert new_text == text and (n_typed, n_hyper) == (0, 1)
    assert new_ents[0].url == "https://t.me/One_Piece_inEnglish/2000" and new_ents[1].url == "https://t.me/another/115"
    assert ents[0].url.endswith("/115")  # the message's own entity objects are left alone


def test_buttons_with_a_link_to_a_post_are_rebuilt_other_buttons_are_kept_as_they_are():
    mk = markup([url_btn("Episode 1", "https://t.me/One_Piece_inEnglish/115"), url_btn("Bot", "https://t.me/goku?start=A")])
    new, n = links({115: 300}).remap_markup(mk)
    assert n == 1
    assert [button_url(b) for b in new.rows[0].buttons] == ["https://t.me/One_Piece_inEnglish/300", "https://t.me/goku?start=A"]
    assert [b.text for b in new.rows[0].buttons] == ["Episode 1", "Bot"]
    same, n = links({999: 1}).remap_markup(mk)
    assert n == 0 and same is mk  # nothing to change: the very same keyboard comes back
    assert links().remap_markup(None) == (None, 0)


def test_links_are_counted_even_before_the_new_ids_are_known():
    text = "https://t.me/One_Piece_inEnglish/5 and https://t.me/other/5"
    ents = [types.MessageEntityTextUrl(0, 5, "https://t.me/c/1234567/6")]
    mk = markup([url_btn("x", "https://t.me/One_Piece_inEnglish/7")])
    assert links().count(text, ents, mk) == 3


def test_a_whole_message_is_relinked_or_reported_unchanged():
    msg = make_msg(
        5, "Next: https://t.me/One_Piece_inEnglish/115", [types.MessageEntityBold(0, 4)],
        markup([url_btn("Go", "https://t.me/One_Piece_inEnglish/116")]),
    )
    assert relink_message(msg, links({1: 2})) is None
    rl = relink_message(msg, links({115: 900, 116: 901}))
    assert rl.text == "Next: https://t.me/One_Piece_inEnglish/900" and (rl.n_typed, rl.n_hyper, rl.n_buttons, rl.links) == (1, 0, 1, 2)
    assert rl.text_changed and rl.markup_changed
    assert button_url(rl.markup.rows[0].buttons[0]).endswith("/901")
    # running it again on the result changes nothing: the new ids are not old ids
    again = make_msg(5, rl.text, rl.entities, rl.markup)
    assert relink_message(again, links({115: 900, 116: 901})) is None


def test_posts_that_moved_twice_point_at_their_latest_copy():
    assert resolve_chain({1: 5, 5: 9, 2: 6}) == {1: 9, 5: 9, 2: 6}
    assert resolve_chain({}) == {}


def test_offsets_use_utf16_units():
    text = "😀😀 https://t.me/One_Piece_inEnglish/3"
    assert len(add_surrogate(text)) == 4 + 1 + len("https://t.me/One_Piece_inEnglish/3")
    new_text, _, n, _ = links({3: 30}).remap_text(text, [])
    assert new_text == "😀😀 https://t.me/One_Piece_inEnglish/30" and n == 1
