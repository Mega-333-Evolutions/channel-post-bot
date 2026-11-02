"""A rich message that was made in Telegram and sent to the bot has to be sent again as blocks - and Telegram refuses blocks it
can't take back (RICH_MESSAGE_BLOCK_INVALID). What the bot converts, how it finds the blocks that are refused, and what the
owner is told."""
import asyncio

from telethon import errors, functions, types

from app.richmsg import (
    Rich,
    block_count,
    block_labels,
    block_name,
    digest,
    fingerprint,
    input_of,
    inputify,
    is_block_invalid,
    pack_blocks,
    plain_of,
    refused_blocks,
    rich_from_received,
    rich_of,
    to_input,
    unpack_blocks,
    without_blocks,
)
from app.tgutil import _b64, explain_rpc

from .fakes import rpc
from .harness import norm
from .test_broadcast import make_app as broadcast_app
from .test_flows import make_app
from .test_markup_flows import new_post
from .test_rich import blocks, rich_msg, same
from .test_rich_flows import native, rich_sends

T = types.TextPlain
PEER = types.InputPeerUser(1, 0)


def block_invalid():
    return errors.BadRequestError(
        functions.messages.SendMessageRequest(peer=types.InputPeerSelf(), message=""), "RICH_MESSAGE_BLOCK_INVALID", 400
    )


def a_map(access_hash=99):
    """A map block the way Telegram hands it out: it holds a stored point."""
    return types.PageBlockMap(
        types.GeoPoint(long=12.5, lat=41.9, access_hash=access_hash, accuracy_radius=7), 14, 600, 300,
        types.PageCaption(T("Rome"), T("")),
    )


def with_bad(n=5, bad=(1, 3)):
    """`n` blocks, the ones numbered `bad` are blocks Telegram will not take back."""
    return [types.PageBlockUnsupported() if i in bad else types.PageBlockParagraph(T(f"block {i}")) for i in range(n)]


class Gate:
    """A Telegram that refuses a rich message of more than `limit` blocks (and takes anything smaller)."""

    def __init__(self, limit=2, error=None):
        self.limit, self.error, self.sent, self.deleted, self.ids = limit, error, [], [], iter(range(500, 900))

    async def __call__(self, req):
        if isinstance(req, functions.messages.SendMessageRequest):
            self.sent.append(req)
            if self.error is not None:
                raise self.error
            if len(req.rich_message.blocks) > self.limit:
                raise block_invalid()
            return types.UpdateShortSentMessage(id=next(self.ids), pts=0, pts_count=0, date=None, out=True)
        if isinstance(req, functions.messages.DeleteMessagesRequest):
            self.deleted += list(req.id)
            return None
        raise NotImplementedError(type(req))


# ================================================================================================= what is converted
def test_a_stored_map_is_turned_into_an_input_map_wherever_it_is():
    cap = types.PageCaption(T("c"), types.TextEmpty())
    detail = types.PageBlockDetails([types.PageBlockParagraph(T("x")), a_map()], T("More"))
    collage = types.PageBlockCollage([a_map()], cap)
    original = [bytes(b) for b in (detail, collage)]
    out = inputify([detail, collage, types.PageBlockParagraph(T("p"))])
    assert [type(b).__name__ for b in out] == ["PageBlockDetails", "PageBlockCollage", "PageBlockParagraph"]
    inner = out[0].blocks[1]
    assert isinstance(inner, types.InputPageBlockMap) and isinstance(out[1].items[0], types.InputPageBlockMap)
    assert (inner.geo.lat, inner.geo.long, inner.geo.accuracy_radius) == (41.9, 12.5, 7)
    assert (inner.zoom, inner.w, inner.h, bytes(inner.caption)) == (14, 600, 300, bytes(a_map().caption))
    assert isinstance(inner.geo, types.InputGeoPoint)
    assert [bytes(b) for b in (detail, collage)] == original  # what was handed to us is left as it was
    assert isinstance(detail.blocks[1], types.PageBlockMap)
    bytes(types.InputRichMessage(blocks=out))  # and it can be sent


def test_blocks_without_a_map_come_back_the_same():
    bl = blocks() + [types.PageBlockDetails(blocks("x"), T("t")), types.PageBlockDivider()]
    assert same(inputify(bl), bl)


def test_a_received_message_with_a_map_is_sent_with_an_input_map_and_keeps_its_fingerprint():
    rm = rich_msg([types.PageBlockHeading1(T("Trip")), a_map(), types.PageBlockParagraph(T("end"))])
    r = rich_from_received(rm)
    sent = to_input(r)
    assert [type(b).__name__ for b in sent.blocks] == ["PageBlockHeading1", "InputPageBlockMap", "PageBlockParagraph"]
    assert [type(b).__name__ for b in input_of(rm).blocks] == [type(b).__name__ for b in sent.blocks]
    assert r.fp == fingerprint(rm)
    assert fingerprint(rich_msg([types.PageBlockHeading1(T("Trip")), a_map(access_hash=12345), types.PageBlockParagraph(T("end"))])) == r.fp


def test_blocks_saved_in_telegrams_own_form_by_an_older_version_are_converted_when_sent():
    old = types.InputRichMessage(blocks=[a_map(), types.PageBlockParagraph(T("end"))])
    sent = to_input(Rich("blocks", _b64(old), False, False, None))
    assert [type(b).__name__ for b in sent.blocks] == ["InputPageBlockMap", "PageBlockParagraph"]
    assert isinstance(old.blocks[0], types.PageBlockMap)  # the saved copy itself is not touched


def test_a_thinking_block_goes_out_as_an_expandable_section():
    thought = types.PageBlockThinking(types.TextBold(T("let me see")))
    nested = types.PageBlockDetails([thought], T("Outer"))
    out = inputify([thought, nested])
    for sec in (out[0], out[1].blocks[0]):
        assert isinstance(sec, types.PageBlockDetails) and bytes(sec.title) == bytes(T("Thinking"))
        (para,) = sec.blocks
        assert isinstance(para, types.PageBlockParagraph) and bytes(para.text) == bytes(types.TextBold(T("let me see")))
    assert isinstance(nested.blocks[0], types.PageBlockThinking)  # what was handed to us is left as it was
    # what the sync compares is the same whichever of the two forms Telegram shows
    assert fingerprint(rich_msg([thought])) == fingerprint(rich_msg([out[0]]))


def test_a_mention_of_somebody_the_message_has_no_access_data_for_stays_the_plain_name():
    mention = types.PageBlockParagraph(types.TextConcat([T("hi "), types.TextMentionName(T("Mia"), 5)]))
    rm = rich_msg([mention])
    plain = to_input(rich_from_received(rm, []))
    assert plain.blocks[0].text.texts[1].__class__ is types.TextPlain and plain.blocks[0].text.texts[1].text == "Mia"
    known = to_input(rich_from_received(rm, [types.InputUser(5, 77)]))
    assert isinstance(known.blocks[0].text.texts[1], types.TextMentionName) and known.blocks[0].text.texts[1].user_id == 5
    assert [u.user_id for u in known.users] == [5]


# ==================================================================================== how the owner is told which blocks
def test_blocks_have_names_and_are_listed_from_one():
    r = rich_from_received(rich_msg([types.PageBlockHeading2(T("a")), a_map(), types.PageBlockTable(T(""), [])]))
    assert [block_name(b) for b in to_input(r).blocks] == ["heading", "map", "table"]
    assert block_name(types.PageBlockUnsupported()) == "a block this version does not know"
    assert block_name(types.InputPageBlockMap(types.InputGeoPointEmpty(), 1, 1, 1, types.PageCaption(T(""), T(""))))== "map"
    assert block_labels(r, [2, 1, 1, 9]) == "2 (map), 3 (table)"
    assert block_count(r) == 3 and block_count(Rich("markdown", "# x")) == 0


def test_block_numbers_fit_in_a_button():
    for idx in ([], [0], [1, 3], [0, 59], list(range(60))):
        word = pack_blocks(idx)
        assert unpack_blocks(word) == sorted(idx) and len(f"rx:123456789:abcdef:{word}") <= 64
    assert pack_blocks([]) == "0"


def test_leaving_blocks_out_keeps_the_rest_and_forgets_the_fingerprint():
    rm = rich_msg(with_bad(4, bad=(2,)), photos=[], documents=[], rtl=True)
    r = rich_from_received(rm)
    kept = without_blocks(r, [2])
    inp = to_input(kept)
    assert [type(b).__name__ for b in inp.blocks] == ["PageBlockParagraph"] * 3 and inp.rtl is True
    assert kept.fp is None and kept.fmt == "blocks" and kept.rtl is True
    assert digest(kept) != digest(r) and block_count(r) == 4 and block_count(kept) == 3


def test_explain_rpc_knows_the_code_that_telegram_gave_for_a_rich_message():
    text = explain_rpc(block_invalid())
    assert text.startswith("Telegram refused (BadRequestError).") and "would not take one of the blocks" in text
    assert "(RICH_MESSAGE_BLOCK_INVALID)" in text
    assert "admin rights" in explain_rpc(rpc("ChatAdminRequiredError"))  # the others are as they were
    other = errors.BadRequestError(None, "SOMETHING_ELSE", 400)
    assert "SOMETHING_ELSE" in explain_rpc(other) and not is_block_invalid(other) and is_block_invalid(block_invalid())


# ================================================================================================ finding the blocks
def refusing_the_unsupported(gate):
    """`gate`, but a message with a block of the unsupported kind is refused (like a block Telegram can't take back)."""

    async def call(req):
        if isinstance(req, functions.messages.SendMessageRequest) and any(
            isinstance(b, types.PageBlockUnsupported) for b in req.rich_message.blocks
        ):
            gate.sent.append(req)
            raise block_invalid()
        return await gate(req)

    return call


def test_the_refused_blocks_are_found_and_the_test_messages_are_deleted():
    async def run():
        gate = Gate(limit=10)
        r = rich_from_received(rich_msg(with_bad(8, bad=(2, 5))))
        assert await refused_blocks(refusing_the_unsupported(gate), PEER, r, pause=0) == [2, 5]
        assert all(q.silent for q in gate.sent) and len(gate.sent) <= 14  # (the whole message is known to be refused: not asked again)
        refused = [q for q in gate.sent if any(isinstance(b, types.PageBlockUnsupported) for b in q.rich_message.blocks)]
        taken = len(gate.sent) - len(refused)
        assert taken > 0 and gate.deleted == list(range(500, 500 + taken))  # every test message that was made is deleted again

    asyncio.run(run())


def test_nothing_is_found_when_every_part_is_taken_or_it_cannot_be_looked_into():
    async def run():
        r = rich_from_received(rich_msg(with_bad(4, bad=())))
        assert await refused_blocks(Gate(limit=2), PEER, r, pause=0) is None  # too long together, fine in parts
        assert await refused_blocks(Gate(limit=2), PEER, Rich("markdown", "# x"), pause=0) is None  # Telegram reads that itself
        assert await refused_blocks(Gate(limit=9), PEER, rich_from_received(rich_msg(with_bad(61, bad=()))), pause=0) is None
        broken = Gate(limit=0, error=rpc("ChatWriteForbiddenError"))  # not a refused block: something else is wrong
        assert await refused_blocks(broken, PEER, rich_from_received(rich_msg(with_bad(3, bad=(1,)))), pause=0) is None

    asyncio.run(run())


def test_a_test_message_that_cannot_be_deleted_does_not_stop_the_search():
    async def run():
        gate = Gate(limit=10)
        inner = refusing_the_unsupported(gate)

        async def call(req):
            if isinstance(req, functions.messages.DeleteMessagesRequest):
                raise rpc("MessageIdInvalidError")
            return await inner(req)

        r = rich_from_received(rich_msg(with_bad(4, bad=(0, 2))))
        assert await refused_blocks(call, PEER, r, pause=0) == [0, 2]

    asyncio.run(run())


# ================================================================================ a forwarded rich message in the bot
MAPPED = [types.PageBlockHeading1(T("Trip")), a_map(), types.PageBlockParagraph(T("end"))]


def test_a_forwarded_message_with_a_map_is_previewed_and_published(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        rm = rich_msg(MAPPED)
        pid, prompt = await new_post(app, "", message=native(rm), publish=False)
        assert "Rich message kept as it is" in prompt
        await app.press(f"pv:{pid}")
        kind, peer, inp, kw = rich_sends(app)[-1]
        assert isinstance(peer, types.InputPeerUser)
        assert [type(b).__name__ for b in inp.blocks] == ["PageBlockHeading1", "InputPageBlockMap", "PageBlockParagraph"]
        assert "refused" not in norm(app.out.last_text or "")
        await app.press(f"pb:{pid}")
        await app.press(f"pby:{pid}")
        post = await app.db.get_post(pid)
        assert post.status == "sent"
        live = app.tg.msgs[post.message_id].rich_message
        assert [type(b).__name__ for b in live.blocks][1] == "InputPageBlockMap"
        assert rich_of(post).fp == fingerprint(rm)  # the sync still knows it
        await app.db.close()

    asyncio.run(run())


def test_a_forwarded_message_with_a_thinking_block_is_previewed_and_published(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        rm = rich_msg([types.PageBlockThinking(T("pondering")), a_map(), types.PageBlockParagraph(T("answer"))])
        pid, _ = await new_post(app, "", message=native(rm), publish=False)
        await app.press(f"pv:{pid}")
        assert "refused" not in norm(app.out.last_text or "")
        assert [type(b).__name__ for b in rich_sends(app)[-1][2].blocks] == [
            "PageBlockDetails", "InputPageBlockMap", "PageBlockParagraph"
        ]
        await app.press(f"pb:{pid}")
        await app.press(f"pby:{pid}")
        post = await app.db.get_post(pid)
        live = app.tg.msgs[post.message_id].rich_message
        assert post.status == "sent" and bytes(live.blocks[0].title) == bytes(T("Thinking"))
        assert rich_of(post).fp == fingerprint(live)  # the sync sees what is in the channel as the saved post
        await app.db.close()

    asyncio.run(run())


def forwarded_with_bad_blocks(app):
    return new_post(app, "", message=native(rich_msg(with_bad(5, bad=(1, 3)))), publish=False)


def test_the_preview_says_which_blocks_telegram_refuses_and_a_draft_can_go_on_without_them(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, _ = await forwarded_with_bad_blocks(app)
        await app.press(f"pv:{pid}")
        text = norm(app.out.last_text)
        assert "Telegram refused (BadRequestError)" in text and "would not take one of the blocks" in text
        assert "It won't take these blocks (of 5): 2 (a block this version does not know), 4 (a block this version does not know). The others are fine." in text
        assert "see /rich for the syntax" not in text  # that is for typed messages
        assert app.out.last_buttons() == [["✂️ Leave those out"], ["↩️ Back"]]
        probes = [e for e in rich_sends(app) if isinstance(e[1], types.InputPeerUser)]
        assert all(e[3]["silent"] for e in probes[1:]) and sorted(app.tg.dm_rich_ids) == sorted(
            i for ids in app.tg.delete_requests for i in ids
        )  # nothing of the check stays in the chat

        await app.press(app.out.callback_data("Leave those out"))
        assert "Left out: 2 (a block this version does not know), 4 (a block this version does not know)." in norm(app.out.last_text)
        post = await app.db.get_post(pid)
        kept = to_input(rich_of(post))
        assert [b.text.text for b in kept.blocks] == ["block 0", "block 2", "block 4"]
        assert post.text == plain_of(rich_of(post)) == "block 0\nblock 2\nblock 4" and post.status == "draft"
        assert len(post.entities) == 1 and post.entities[0]["k"] == "rich" and "p" not in post.entities[0]

        before = len(rich_sends(app))
        await app.press(f"pv:{pid}")  # now Telegram takes it
        assert len(rich_sends(app)) == before + 1 and len(rich_sends(app)[-1][2].blocks) == 3
        await app.press(f"pb:{pid}")
        await app.press(f"pby:{pid}")
        post = await app.db.get_post(pid)
        assert post.status == "sent" and len(app.tg.msgs[post.message_id].rich_message.blocks) == 3
        await app.db.close()

    asyncio.run(run())


def test_publishing_a_message_telegram_refuses_names_the_blocks_and_publishes_nothing(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, _ = await forwarded_with_bad_blocks(app)
        await app.press(f"pb:{pid}")
        await app.press(f"pby:{pid}")
        text = norm(app.out.last_text)
        assert "It won't take these blocks (of 5): 2 (" in text and "The post was NOT published." in text
        assert "Leave those out" in str(app.out.last_buttons())
        assert (await app.db.get_post(pid)).status == "draft" and app.tg.msgs == {}
        await app.press(app.out.callback_data("Leave those out"))
        await app.press(f"pb:{pid}")
        await app.press(f"pby:{pid}")
        assert (await app.db.get_post(pid)).status == "sent" and len(app.tg.msgs) == 1
        await app.db.close()

    asyncio.run(run())


def test_the_leave_out_button_is_only_for_the_draft_that_was_checked(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, _ = await forwarded_with_bad_blocks(app)
        await app.press(f"pv:{pid}")
        data = app.out.callback_data("Leave those out")
        # the post is changed meanwhile: the old button must not cut blocks out of the new message
        await app.press(f"et:{pid}")
        await app.text("", message=native(rich_msg(with_bad(6, bad=(0,)))))
        changed = (await app.db.get_post(pid)).entities
        await app.press(data)
        assert "not the one that was checked any more" in norm(app.out.last_text)
        assert (await app.db.get_post(pid)).entities == changed
        # a post that is published is not edited this way
        await app.press(f"pv:{pid}")
        data = app.out.callback_data("Leave those out")
        await app.db.update_post(pid, status="sent")
        await app.press(data)
        assert "not the one that was checked any more" in norm(app.out.last_text)
        assert (await app.db.get_post(pid)).entities == changed
        await app.db.close()

    asyncio.run(run())


def test_when_every_block_is_refused_nothing_is_offered_to_leave_out(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, _ = await new_post(app, "", message=native(rich_msg(with_bad(3, bad=(0, 1, 2)))), publish=False)
        await app.press(f"pv:{pid}")
        text = norm(app.out.last_text)
        assert "It won't take any block of that message as it is" in text and "/rich" in text
        assert "Leave those out" not in text and "(of 3)" not in text
        await app.db.close()

    asyncio.run(run())


def test_a_refusal_that_is_not_about_a_block_is_not_looked_into(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, _ = await forwarded_with_bad_blocks(app)
        app.tg.fail["SendMessageRequest"] = rpc("ChatWriteForbiddenError")
        await app.press(f"pv:{pid}")
        text = norm(app.out.last_text)
        assert "ChatWriteForbiddenError" in text and "Telegram could not make that rich message." in text
        assert "see /rich for the syntax" not in text and "blocks (of" not in text
        assert len(rich_sends(app)) == 1  # no test messages were sent
        await app.db.close()

    asyncio.run(run())


def test_a_typed_rich_message_that_is_refused_still_points_to_the_syntax(tmp_path):
    async def run():
        app = await make_app(tmp_path)
        pid, _ = await new_post(app, "<rich>\n<tg-map lat=\"x\"/>", publish=False)
        app.tg.fail["SendMessageRequest"] = block_invalid()
        await app.press(f"pv:{pid}")
        text = norm(app.out.last_text)
        assert "would not take one of the blocks" in text and "see /rich for the syntax" in text
        assert len(rich_sends(app)) == 1  # (Telegram reads the typed text itself: there are no blocks to try)
        await app.db.close()

    asyncio.run(run())


def test_a_broadcast_of_a_message_telegram_refuses_says_why(tmp_path):
    async def run():
        app = await broadcast_app(tmp_path)
        await app.text("/broadcast 1h")
        await app.text("", message=native(rich_msg(with_bad(3, bad=(1,)))))
        await app.press(app.out.callback_data("No buttons"))
        text = norm(app.out.last_text)
        assert "would not take one of the blocks" in text and "can't be sent like this" in text
        assert "It won't take these blocks (of 3): 2 (a block this version does not know)." in text
        assert all(not chan.msgs for chan in app.tg.registry.values())
        await app.db.close()

    asyncio.run(run())
