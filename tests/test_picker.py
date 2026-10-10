"""The channel chooser: names written out as a numbered list (A to Z, 20 to a page), buttons that carry only numbers
(5 to a row), ◀ ▶ underneath - and every command that needs a channel uses it."""
import asyncio
from types import SimpleNamespace

from app import picker
from app.picker import PER_PAGE, PER_ROW, names_of, natural_key, ordered, render

from .harness import App, norm


def run(coro):
    return asyncio.run(coro)


def chan(i, title, username=None):
    return SimpleNamespace(id=i, title=title, username=username)


def data_of(btn):
    t = btn.type if hasattr(btn, "type") else btn
    return t.data.decode()


def lines_of(text):
    """The numbered lines of a message: ['1. Naruto', ...]."""
    return [ln for ln in norm(text).splitlines() if ln[:1].isdigit() and ". " in ln[:5]]


# ------------------------------------------------------------------------------------------ the list itself
def test_names_are_sorted_a_to_z_ignoring_case_and_numbers_count_by_value():
    chans = [chan(1, "One piece"), chan(2, "naruto"), chan(3, "Dragon ball"), chan(4, "Anime 10"), chan(5, "Anime 2")]
    assert [c.title for c in ordered(chans)] == ["Anime 2", "Anime 10", "Dragon ball", "naruto", "One piece"]
    assert natural_key("  Anime   2 ") == natural_key("anime 2")
    # the same name twice: the id decides, so the order never changes between two looks
    assert [c.id for c in ordered([chan(9, "Same"), chan(4, "same")])] == [4, 9]


def test_the_message_lists_the_names_and_the_buttons_only_carry_numbers():
    chans = [chan(10, "One piece"), chan(20, "Naruto"), chan(30, "Dragon ball")]
    text, rows = render(chans, 0, head="Which one?", kind="x", choose=lambda c: f"go:{c.id}")
    assert lines_of(text) == ["1. Dragon ball", "2. Naruto", "3. One piece"]
    assert [[b.text for b in row] for row in rows] == [["1", "2", "3"]]
    # pressing 3 means One piece - the button carries the channel's id, not its place in the list
    assert [data_of(b) for b in rows[0]] == ["go:30", "go:20", "go:10"]
    assert text.startswith("Which one?") and "Tap the number of the channel." in text
    assert "Page" not in text  # one page: no page line, no arrows


def test_a_row_has_at_most_five_buttons_and_the_extra_rows_come_last():
    chans = [chan(i, f"Channel {i:02d}") for i in range(1, 13)]
    text, rows = render(chans, 0, head="h", kind="x", choose=lambda c: f"go:{c.id}", extra_rows=[["cancel-row"]])
    assert [len(r) for r in rows[:-1]] == [5, 5, 2]
    assert rows[-1] == ["cancel-row"]
    assert PER_ROW == 5 and PER_PAGE == 20


def test_twenty_names_to_a_page_with_arrows_that_wrap_around():
    chans = [chan(i, f"Channel {i:02d}") for i in range(1, 46)]
    text0, rows0 = render(chans, 0, head="h", kind="x", arg="tok", choose=lambda c: f"go:{c.id}", extra_rows=[["cancel-row"]])
    assert [ln.split(". ")[0] for ln in lines_of(text0)] == [str(n) for n in range(1, 21)]
    assert "Page 1 of 3 - channels 1 to 20 of 45" in norm(text0)
    assert [[b.text for b in r] for r in rows0[:4]] == [[str(n) for n in range(s, s + 5)] for s in (1, 6, 11, 16)]
    assert [b.text for b in rows0[4]] == ["◀", "▶"]  # the arrows sit under the number buttons
    assert rows0[5] == ["cancel-row"]
    assert data_of(rows0[4][1]) == "cpg:x:tok:1" and data_of(rows0[4][0]) == "cpg:x:tok:2"  # ◀ on the first page = the last page

    text1, rows1 = render(chans, 1, head="h", kind="x", arg="tok", choose=lambda c: f"go:{c.id}")
    assert lines_of(text1)[0] == "21. Channel 21" and lines_of(text1)[-1] == "40. Channel 40"
    assert [[b.text for b in r] for r in rows1[:4]] == [[str(n) for n in range(s, s + 5)] for s in (21, 26, 31, 36)]
    assert data_of(rows1[0][0]) == "go:21" and data_of(rows1[3][4]) == "go:40"
    assert data_of(rows1[4][0]) == "cpg:x:tok:0" and data_of(rows1[4][1]) == "cpg:x:tok:2"

    text2, rows2 = render(chans, 2, head="h", kind="x", arg="tok", choose=lambda c: f"go:{c.id}")
    assert lines_of(text2) == [f"{n}. Channel {n:02d}" for n in range(41, 46)]
    assert [[b.text for b in r] for r in rows2[:1]] == [["41", "42", "43", "44", "45"]]
    assert data_of(rows2[1][1]) == "cpg:x:tok:0"  # ▶ on the last page = the first page
    # a page number that no longer exists (channels were removed meanwhile) shows the last page
    assert lines_of(render(chans, 99, head="h", kind="x", choose=lambda c: "go")[0])[0] == "41. Channel 41"


def test_a_list_to_read_has_no_number_buttons_but_still_pages():
    chans = [chan(i, f"Channel {i:02d}") for i in range(1, 26)]
    text, rows = render(chans, 0, head="h", kind="ch", choose=None)
    assert [[b.text for b in r] for r in rows] == [["◀", "▶"]]
    assert "Tap the number" not in text
    assert render(chans[:3], 0, head="h", kind="ch", choose=None)[1] == []


def test_equal_names_get_the_username_or_id_and_odd_names_are_safe():
    chans = [chan(1, "Anime", "animech"), chan(2, "ANIME"), chan(3, "Other", "otherch")]
    assert names_of(ordered(chans)) == ["Anime (@animech)", "ANIME (2)", "Other"]
    assert names_of(ordered(chans), with_username=True) == ["Anime (@animech)", "ANIME (2)", "Other (@otherch)"]
    long = chan(4, "x" * 200)
    assert len(names_of([long])[0]) == picker.NAME_MAX
    text, _ = render([chan(5, "<b>Bold & co</b>")], 0, head="h", kind="x", choose=lambda c: "go")
    assert "&lt;b&gt;Bold &amp; co&lt;/b&gt;" in text and "<b>Bold" not in text.split("\n\n", 1)[1]
    assert names_of([chan(6, "")]) == ["Channel"]


# ------------------------------------------------------------------------------ the commands that use it
async def make_app(tmp_path, n, *, owner=1, **cfg):
    app = App(tmp_path, owner=owner, edit_delay=0, **cfg)
    await app.start()
    for i in reversed(range(1, n + 1)):  # saved in reverse: the list has to sort them
        await app.db.save_channel(i, 5, f"Channel {i:02d}", f"ch{i:02d}", 1)
    return app


def test_new_lists_the_channels_and_a_number_starts_a_post_for_that_channel(tmp_path):
    async def go():
        app = await make_app(tmp_path, 45)
        await app.text("/new")
        text = norm(app.out.last_text)
        assert "Which channel is this post for?" in text
        assert lines_of(app.out.last_text)[0] == "1. Channel 01" and len(lines_of(app.out.last_text)) == 20
        assert [len(r) for r in app.out.last_buttons()] == [5, 5, 5, 5, 2, 1]  # 4 rows of numbers, the arrows, Cancel
        await app.press(app.out.callback_exact("▶"))  # edits the same message
        assert lines_of(app.out.last_text)[0] == "21. Channel 21" and lines_of(app.out.last_text)[-1] == "40. Channel 40"
        assert app.out.last_buttons()[0] == ["21", "22", "23", "24", "25"]
        await app.press(app.out.callback_exact("▶"))
        assert lines_of(app.out.last_text) == [f"{n}. Channel {n:02d}" for n in range(41, 46)]
        await app.press(app.out.callback_exact("▶"))  # wraps round to the first page
        assert lines_of(app.out.last_text)[0] == "1. Channel 01"
        await app.press(app.out.callback_exact("◀"))  # ... and back to the last one
        assert lines_of(app.out.last_text)[0] == "41. Channel 41"
        await app.press(app.out.callback_exact("43"))
        assert app.ctx.state[1]["mode"] == "new_content" and app.ctx.state[1]["cid"] == 43
        await app.db.close()

    run(go())


def test_new_with_a_single_channel_still_goes_straight_to_the_post(tmp_path):
    async def go():
        app = await make_app(tmp_path, 1)
        await app.text("/new")
        assert app.ctx.state[1]["cid"] == 1 and "Send the post now" in norm(app.out.last_text)
        await app.db.close()

    run(go())


def test_posts_offers_the_channels_and_the_drafts(tmp_path):
    async def go():
        app = await make_app(tmp_path, 25)
        await app.text("/posts")
        assert "My posts" in norm(app.out.last_text) and lines_of(app.out.last_text)[0] == "1. Channel 01"
        assert app.out.last_buttons()[-1] == ["💾 Drafts (all channels)"]
        assert app.out.callback_exact("💾 Drafts (all channels)") == "pc:d:0"
        assert app.out.callback_exact("3") == "pc:3:0"
        await app.press(app.out.callback_exact("▶"))
        assert lines_of(app.out.last_text) == [f"{n}. Channel {n:02d}" for n in range(21, 26)]
        assert app.out.last_buttons()[-1] == ["💾 Drafts (all channels)"]
        await app.press(app.out.callback_exact("24"))
        assert "Channel 24" in norm(app.out.last_text) and "post(s)" in norm(app.out.last_text)
        await app.press("pl")  # 🔙 Channels
        assert lines_of(app.out.last_text)[0] == "1. Channel 01"
        await app.db.close()

    run(go())


def test_posts_without_a_channel_still_offers_the_drafts(tmp_path):
    async def go():
        app = await make_app(tmp_path, 0)
        await app.text("/posts")
        assert "No channels yet" in norm(app.out.last_text) and app.out.callback_exact("💾 Drafts (all channels)") == "pc:d:0"
        await app.db.close()

    run(go())


def test_channels_owner_taps_a_number_and_confirms_before_a_channel_is_removed(tmp_path):
    async def go():
        app = await make_app(tmp_path, 3)
        await app.text("/channels")
        assert lines_of(app.out.last_text) == ["1. Channel 01 (@ch01)", "2. Channel 02 (@ch02)", "3. Channel 03 (@ch03)"]
        assert app.out.last_buttons() == [["1", "2", "3"]]
        await app.press(app.out.callback_exact("2"))
        assert "Take Channel 02 out of the bot?" in norm(app.out.last_text)
        assert [c.id for c in await app.db.list_channels()] == [1, 2, 3]  # nothing is removed by the number alone
        await app.press(app.out.callback_exact("🔙 Back"))
        assert lines_of(app.out.last_text)[1] == "2. Channel 02 (@ch02)"
        await app.press(app.out.callback_exact("2"))
        await app.press(app.out.callback_exact("✅ Yes, remove it"))
        assert "removed from the bot" in norm(app.out.last_text)
        assert [c.id for c in await app.db.list_channels()] == [1, 3]
        await app.db.close()

    run(go())


def test_channels_for_an_admin_is_only_a_list(tmp_path):
    async def go():
        app = await make_app(tmp_path, 3, owner=7, admins=frozenset({1}))
        app.uid = 1  # an admin, not an owner
        await app.text("/channels")
        assert lines_of(app.out.last_text)[0] == "1. Channel 01 (@ch01)" and app.out.last_buttons() == []
        assert "tap its number" not in norm(app.out.last_text)
        await app.press("chd:2")  # a made-up press does nothing for a non-owner
        assert [c.id for c in await app.db.list_channels()] == [1, 2, 3]
        await app.db.close()

    run(go())


def test_autopost_and_repost_page_through_the_channels_and_a_number_picks_one(tmp_path):
    async def go():
        app = await make_app(tmp_path, 45)
        await app.text("/autopost 20 10")
        assert "Which channel should I post them in?" in norm(app.out.last_text) and "2 post(s)" in norm(app.out.last_text)
        assert lines_of(app.out.last_text)[0] == "1. Channel 01"
        await app.press(app.out.callback_exact("▶"))
        assert "2 post(s)" in norm(app.out.last_text) and lines_of(app.out.last_text)[0] == "21. Channel 21"  # same question
        assert app.out.callback_exact("21").startswith("apc:") and app.out.callback_exact("21").endswith(":21")
        assert app.out.last_buttons()[-1] == ["✖️ Cancel"]

        await app.text("/repost")
        await app.text("/repost --last 3")
        assert "Which channel should I repost?" in norm(app.out.last_text) and "newest 3" in norm(app.out.last_text)
        await app.press(app.out.callback_exact("▶"))
        await app.press(app.out.callback_exact("▶"))
        assert lines_of(app.out.last_text)[0] == "41. Channel 41" and "newest 3" in norm(app.out.last_text)
        data = app.out.callback_exact("45")
        assert data.startswith("rpch:") and data.endswith(":45")
        await app.db.close()

    run(go())


def test_an_old_arrow_of_a_list_that_has_expired_says_so(tmp_path):
    async def go():
        app = await make_app(tmp_path, 25)
        await app.press("cpg:rp:deadbeef:1")
        assert "expired" in norm(app.out.last_text)
        await app.press("cpg:ap:deadbeef:1")
        assert "expired" in norm(app.out.last_text)
        await app.press("cpg:zz::1")
        assert "no longer active" in norm(app.out.last_text)
        await app.db.close()

    run(go())


def test_the_channel_pickers_of_the_owner_tools_refuse_everybody_else(tmp_path):
    async def go():
        app = await make_app(tmp_path, 25, owner=7, admins=frozenset({1}))
        app.uid = 1
        await app.press("cpg:rp:deadbeef:1")
        assert "Only the bot owner" in norm(app.out.last_text)
        await app.db.close()

    run(go())
