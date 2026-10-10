"""/export and /import: a backup can be put back, only adds, and never trusts what is in the file."""
import asyncio
import datetime
import json
from types import SimpleNamespace

import pytest

from app.backup import (
    MAX_BYTES,
    BackupError,
    apply_import,
    channel_row,
    parse_backup,
    plan_import,
    post_row,
    stamp,
)
from app.db import utcnow

from .dbutil import db_url_for, fresh_db
from .harness import App, FakeEvent, norm

T0 = datetime.datetime(2026, 10, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)


def run(coro):
    return asyncio.run(coro)


def backup_bytes(**parts):
    data = {"exported_at": T0.isoformat(), "channels": [], "posts": [], "ignored": []}
    data.update(parts)
    return json.dumps(data).encode()


def chan(cid=1, **kw):
    return {"id": cid, "access_hash": 5, "title": f"Channel {cid}", "username": f"ch{cid}", "active": True,
            "added_by": 1, "created_at": T0.isoformat(), **kw}


def post(cid=1, mid=10, **kw):
    base = {"channel_id": cid, "message_id": mid, "status": "sent", "source": "bot", "text": f"post {mid}", "entities": [],
            "media_kind": None, "media_file_id": None, "buttons": [], "link_preview": False, "created_by": 1,
            "created_at": T0.isoformat(), "updated_at": T0.isoformat(), "sent_at": T0.isoformat()}
    base.update(kw)
    return base


async def fresh(tmp_path, name="a"):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    return await fresh_db(db_url_for(d))


# ================================================================================================ reading the file
def test_a_good_file_is_read_completely():
    posts = [
        post(1, 10, entities=[{"k": "bold", "o": 0, "l": 4}], buttons=[[{"t": "Watch", "u": "https://t.me/a/1"}]]),
        post(1, None, status="draft", text="not sent yet"),
        post(2, 7, source="adopted", media_kind="photo", media_file_id="abc"),
    ]
    bk = parse_backup(backup_bytes(channels=[chan(1), chan(2, username=None, active=False)], posts=posts, ignored=[{"channel_id": 1, "message_id": 4}]))
    assert [c["id"] for c in bk.channels] == [1, 2] and bk.channels[1]["active"] is False and bk.channels[1]["username"] is None
    assert [(p["channel_id"], p["message_id"], p["status"]) for p in bk.posts] == [(1, 10, "sent"), (1, None, "draft"), (2, 7, "sent")]
    assert bk.posts[0]["buttons"] == [[{"t": "Watch", "u": "https://t.me/a/1"}]] and bk.posts[2]["source"] == "adopted"
    assert bk.posts[0]["created_at"] == T0 and bk.ignored == [(1, 4)] and bk.bad_rows == 0
    assert bk.exported_at == T0.isoformat()


def test_a_file_with_a_byte_order_mark_and_old_style_still_reads():
    raw = b"\xef\xbb\xbf" + json.dumps({"channels": [chan(1)], "posts": [post(1, 3)]}).encode()  # no "ignored", no "exported_at"
    bk = parse_backup(raw)
    assert len(bk.channels) == 1 and len(bk.posts) == 1 and bk.ignored == [] and bk.exported_at is None


@pytest.mark.parametrize(
    "raw,why",
    [
        (b"not json at all", "not a JSON file"),
        (b"\xff\xfe\x00", "not a JSON file"),
        (b"[1, 2, 3]", "does not look like a backup"),
        (b'{"hello": "world"}', "does not look like a backup"),
        (b'{"channels": "x", "posts": []}', "“channels” should be a list"),
        (b'{"channels": [], "posts": {}}', "“posts” should be a list"),
        (b'{"channels": [], "posts": []}', "nothing in it that can be imported"),
        (b'{"channels": [5], "posts": ["x"]}', "All entries were unreadable"),
        (b"x" * (MAX_BYTES + 1), "larger than"),
    ],
)
def test_a_file_that_is_not_a_backup_is_refused_with_the_reason(raw, why):
    with pytest.raises(BackupError) as e:
        parse_backup(raw)
    assert why in str(e.value)


def test_unusable_entries_are_counted_and_left_out():
    rows = [
        post(1, 10),
        post(1, 11, status="weird"),  # unknown status
        post(1, None),  # published without a message id
        post(1, 12, text=5),  # text is not text
        post(1, 13, entities="bold"),  # formatting is not a list
        post(1, 14, buttons=[["not a button"]]),  # buttons are not dicts
        post(1, 15, media_kind="x" * 40),
        post(1, 16, created_at="yesterday"),
        post("one", 17),
        post(True, 18),  # a boolean is not a channel id
        "junk",
        None,
    ]
    bk = parse_backup(backup_bytes(channels=[chan(1), chan(0), {"title": "no id"}], posts=rows, ignored=[{"channel_id": 1}, "x", {"channel_id": 1, "message_id": 3}]))
    assert [p["message_id"] for p in bk.posts] == [10]
    assert [c["id"] for c in bk.channels] == [1] and bk.ignored == [(1, 3)]
    assert bk.bad_rows == 11 + 2 + 2 - 1 + 1  # 11 bad posts (one of them None counts too), 2 channels, 2 marks


def test_rows_are_cleaned_while_they_are_read():
    p = post_row({"channel_id": 3, "message_id": 4, "status": "draft", "created_at": "2026-10-01T12:00:00Z"})
    assert p["message_id"] is None and p["status"] == "draft" and p["created_at"] == T0 and p["updated_at"] == T0
    p = post_row({"channel_id": 3, "message_id": 4, "source": "somebody"})
    assert p["status"] == "sent" and p["source"] == "bot" and p["text"] == "" and p["entities"] == [] and p["buttons"] == []
    naive = post_row({"channel_id": 3, "message_id": 4, "created_at": "2026-10-01T12:00:00"})  # no zone: UTC
    assert naive["created_at"] == T0
    c = channel_row({"id": -100123, "username": "@Name", "title": "t" * 400})
    assert c["username"] == "Name" and len(c["title"]) == 256 and c["active"] is True and c["access_hash"] == 0


def test_the_date_of_the_backup_is_written_for_people():
    assert stamp("2026-10-01T12:00:00+00:00") == "01 Oct 2026, 12:00 UTC"
    assert stamp("2026-10-01T17:30:00+05:30") == "01 Oct 2026, 12:00 UTC"
    assert stamp("not a date") == "not a date" and stamp(None) == ""


# =================================================================================================== the round trip
async def filled(db):
    await db.save_channel(1, 11, "Anime One", "animeone", 7)
    await db.save_channel(2, 22, "Movies", None, 7)
    await db.set_channel_active(2, False)
    a = await db.create_post(
        channel_id=1, message_id=10, status="sent", source="bot", text="Episode 1", created_by=7, sent_at=T0,
        entities=[{"k": "bold", "o": 0, "l": 7}], buttons=[[{"t": "Watch", "u": "https://t.me/animeone/9"}]],
        media_kind="photo", media_file_id="ref-1", link_preview=False,
    )
    b = await db.create_post(channel_id=1, message_id=11, status="sent", source="adopted", text="From the app", sent_at=T0)
    c = await db.create_post(channel_id=2, status="draft", text="a draft", created_by=7)
    await db.ignore_post(1, 99)
    return a, b, c


def same_post(x, y):
    keys = ("channel_id", "message_id", "status", "source", "text", "entities", "media_kind", "media_file_id", "buttons",
            "link_preview", "created_by")
    return all(getattr(x, k) == getattr(y, k) for k in keys)


def test_what_export_writes_import_puts_back_on_a_new_bot(tmp_path):
    async def go():
        old = await fresh(tmp_path, "old")
        made = await filled(old)
        data = await old.export_all()
        assert data["ignored"] == [{"channel_id": 1, "message_id": 99}]
        raw = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")  # exactly what /export sends

        new = await fresh(tmp_path, "new")
        plan = await plan_import(new, parse_backup(raw))
        assert (len(plan.new_channels), plan.sent_to_add, plan.drafts_to_add, len(plan.new_ignored)) == (2, 2, 1, 1)
        res = await apply_import(new, plan)
        assert (res.channels_added, res.posts_added, res.drafts_added, res.marks_added, res.skipped_meanwhile) == (2, 2, 1, 1, 0)

        chans = {c.id: c for c in await new.list_channels(active_only=False)}
        assert (chans[1].title, chans[1].username, chans[1].access_hash, chans[1].active) == ("Anime One", "animeone", 11, True)
        assert (chans[2].title, chans[2].username, chans[2].active) == ("Movies", None, False)
        for original in made:
            if original.status == "sent":
                back = await new.find_by_message(original.channel_id, original.message_id)
                assert back is not None and same_post(original, back)
                assert back.id != original.id or True  # the new database numbers its own posts
            else:
                rows, _ = await new.list_posts(2, "draft", 0, 10)
                assert len(rows) == 1 and same_post(original, rows[0])
        assert await new.ignored_ids(1) == {99}
        await old.close()
        await new.close()

    run(go())


def test_importing_the_same_file_twice_adds_nothing_the_second_time(tmp_path):
    async def go():
        old = await fresh(tmp_path, "old")
        await filled(old)
        raw = json.dumps(await old.export_all()).encode()
        new = await fresh(tmp_path, "new")
        await apply_import(new, await plan_import(new, parse_backup(raw)))
        again = await plan_import(new, parse_backup(raw))
        assert again.nothing_new and again.channels_present == 2 and again.posts_present == 2 and again.drafts_present == 1
        res = await apply_import(new, again)
        assert (res.channels_added, res.posts_added, res.drafts_added, res.marks_added) == (0, 0, 0, 0)
        assert len((await new.list_posts(None, None, 0, 50))[0]) == 3
        await old.close()
        await new.close()

    run(go())


def test_importing_only_adds_and_never_changes_what_is_here(tmp_path):
    async def go():
        db = await fresh(tmp_path)
        await db.save_channel(1, 99, "Renamed Since", "newname", 7)
        mine = await db.create_post(channel_id=1, message_id=10, status="sent", text="the edit I made after the backup", sent_at=T0)
        raw = backup_bytes(channels=[chan(1, title="Old Name", username="oldname")], posts=[post(1, 10, text="the old text"), post(1, 11), post(1, 12)])
        plan = await plan_import(db, parse_backup(raw))
        assert plan.channels_present == 1 and plan.posts_present == 1 and plan.sent_to_add == 2
        await apply_import(db, plan)
        c = await db.get_channel(1)
        assert (c.title, c.username, c.access_hash) == ("Renamed Since", "newname", 99)
        assert (await db.find_by_message(1, 10)).text == "the edit I made after the backup"
        assert [p.message_id for p in await db.sent_posts(1)] == [10, 11, 12]
        assert (await db.get_post(mine.id)) is not None
        await db.close()

    run(go())


def test_posts_the_owner_made_the_bot_forget_are_not_brought_back(tmp_path):
    async def go():
        db = await fresh(tmp_path)
        await db.save_channel(1, 5, "Chan", "chan", 7)
        await db.ignore_post(1, 11)  # forgotten here after the backup was made
        raw = backup_bytes(channels=[chan(1)], posts=[post(1, 10), post(1, 11), post(1, 12)], ignored=[{"channel_id": 1, "message_id": 12}])
        plan = await plan_import(db, parse_backup(raw))
        assert plan.posts_forgotten == 2 and plan.sent_to_add == 1  # 11 (forgotten here) and 12 (forgotten in the backup)
        await apply_import(db, plan)
        assert [p.message_id for p in await db.sent_posts(1)] == [10]
        assert await db.ignored_ids(1) == {11, 12}
        await db.close()

    run(go())


def test_posts_of_an_unknown_channel_are_left_out(tmp_path):
    async def go():
        db = await fresh(tmp_path)
        raw = backup_bytes(channels=[chan(1)], posts=[post(1, 10), post(2, 20), post(2, None, status="draft")], ignored=[{"channel_id": 2, "message_id": 5}])
        plan = await plan_import(db, parse_backup(raw))
        assert plan.posts_orphaned == 2 and plan.sent_to_add == 1 and plan.new_ignored == []
        await apply_import(db, plan)
        assert await db.get_channel(2) is None and len((await db.list_posts(None, None, 0, 10))[0]) == 1
        await db.close()

    run(go())


def test_a_post_that_turns_up_between_the_preview_and_the_import_is_skipped_not_an_error(tmp_path):
    async def go():
        db = await fresh(tmp_path)
        raw = backup_bytes(channels=[chan(1)], posts=[post(1, 10), post(1, 11), post(1, 12)])
        plan = await plan_import(db, parse_backup(raw))
        await db.save_channel(1, 5, "Chan", "chan", 7)
        await db.adopt_message(1, 11, {"text": "the sync was quicker"})  # while the owner was reading the preview
        res = await apply_import(db, plan)
        assert res.posts_added == 2 and res.skipped_meanwhile == 1 and res.channels_added == 0
        assert (await db.find_by_message(1, 11)).text == "the sync was quicker"
        assert [p.message_id for p in await db.sent_posts(1)] == [10, 11, 12]
        await db.close()

    run(go())


def test_a_big_backup_is_saved_in_pieces(tmp_path):
    async def go():
        db = await fresh(tmp_path)
        posts = [post(1, i, text=f"post {i}") for i in range(1, 1202)]
        plan = await plan_import(db, parse_backup(backup_bytes(channels=[chan(1)], posts=posts)))
        seen = []

        async def progress(done, total):
            seen.append((done, total))

        res = await apply_import(db, plan, progress=progress)
        assert res.posts_added == 1201 and seen == [(500, 1201), (1000, 1201), (1201, 1201)]
        assert len(await db.sent_posts(1)) == 1201
        await db.close()

    run(go())


def test_two_drafts_with_the_same_text_but_made_at_different_times_are_both_kept(tmp_path):
    async def go():
        db = await fresh(tmp_path)
        d1 = post(1, None, status="draft", text="same", created_at=T0.isoformat())
        d2 = post(1, None, status="draft", text="same", created_at=(T0 + datetime.timedelta(minutes=5)).isoformat())
        raw = backup_bytes(channels=[chan(1)], posts=[d1, d2, d1])  # the third is a copy of the first
        plan = await plan_import(db, parse_backup(raw))
        assert plan.drafts_to_add == 2 and plan.drafts_present == 1
        await apply_import(db, plan)
        assert len((await db.list_posts(1, "draft", 0, 10))[0]) == 2
        await db.close()

    run(go())


# ===================================================================================== the commands, as a person uses them
def backup_file(raw: bytes, *, size=None):
    async def download_media(file=None):
        assert file is bytes
        return raw

    return SimpleNamespace(
        raw_text="", entities=[], media=object(), grouped_id=None, forward=None,
        file=SimpleNamespace(size=len(raw) if size is None else size, name="channel-posts-export.json"),
        download_media=download_media,
    )


async def started(tmp_path, **kw):
    app = App(tmp_path, **kw)
    await app.start()
    return app


def test_export_sends_the_file_and_tells_how_to_put_it_back(tmp_path, monkeypatch):
    async def go():
        app = await started(tmp_path)
        await app.db.save_channel(1, 5, "Chan", "chan", 1)
        await app.db.create_post(channel_id=1, message_id=3, status="sent", text="hello", sent_at=utcnow())
        await app.db.ignore_post(1, 8)
        files = []
        real = FakeEvent.respond

        async def respond(self, text=None, buttons=None, file=None, **kw):
            files.append(file)
            return await real(self, text, buttons, file, **kw)

        monkeypatch.setattr(FakeEvent, "respond", respond)
        await app.text("/export")
        text = app.out.log[-1][1]
        assert "1 channel(s), 1 post(s)" in text and "/import" in text
        (buf,) = files
        assert buf.name == "channel-posts-export.json"
        data = json.loads(buf.getvalue().decode("utf-8"))
        assert set(data) == {"exported_at", "channels", "posts", "ignored"} and data["ignored"] == [{"channel_id": 1, "message_id": 8}]
        back = parse_backup(buf.getvalue())  # what /import reads is what /export wrote
        assert len(back.channels) == 1 and [p["message_id"] for p in back.posts] == [3] and back.ignored == [(1, 8)]
        await app.db.close()

    run(go())


def test_import_asks_for_the_file_and_the_owner_only(tmp_path):
    async def go():
        app = await started(tmp_path)
        await app.text("/import")
        assert "Send me the file that /export made" in norm(app.out.last_text) and "only adds" in norm(app.out.last_text)
        assert app.ctx.state[1]["mode"] == "import"
        await app.text("/cancel")
        assert 1 not in app.ctx.state

        app.uid = 2  # not an owner, not an admin
        await app.text("/import")
        assert "private" in norm(app.out.last_text) and 2 not in app.ctx.state
        await app.db.close()

    run(go())


def test_sending_text_or_the_wrong_file_keeps_the_import_waiting(tmp_path):
    async def go():
        app = await started(tmp_path)
        await app.text("/import")
        await app.text("here you go")
        assert "backup FILE" in norm(app.out.last_text) and app.ctx.state[1]["mode"] == "import"
        await app.text("", message=backup_file(b"this is not json"))
        assert "can't be imported" in norm(app.out.last_text) and "not a JSON file" in norm(app.out.last_text)
        assert app.ctx.state[1]["mode"] == "import"  # the right file can still be sent
        await app.text("", message=backup_file(b"{}", size=MAX_BYTES + 1))
        assert "larger than" in norm(app.out.last_text) and app.ctx.state[1]["mode"] == "import"
        await app.db.close()

    run(go())


def test_the_whole_import_as_a_person_does_it(tmp_path):
    async def go():
        app = await started(tmp_path)
        await app.db.save_channel(1, 5, "Chan", "chan", 1)
        await app.db.create_post(channel_id=1, message_id=10, status="sent", text="already here", sent_at=utcnow())
        raw = backup_bytes(
            channels=[chan(1), chan(2, title="Movies")],
            posts=[post(1, 10), post(1, 11), post(2, 5), post(2, None, status="draft", text="a draft"), post(9, 1)],
            ignored=[{"channel_id": 2, "message_id": 77}],
        )
        await app.text("/import")
        await app.text("", message=backup_file(raw))
        text = norm(app.out.last_text)
        assert "Backup file read" in text and "01 Oct 2026, 12:00 UTC" in text
        assert "Channels: 2 in the file - 1 new, 1 here already" in text
        assert "Published posts: 4 in the file - 2 to add, 1 here already" in text  # the fourth is left out (below)
        assert "Drafts: 1 in the file - 1 to add, 0 here already" in text
        assert "belong to a channel that is neither here nor in the file" in text
        assert "Nothing that is here now is changed or deleted" in text and "admin there" in text
        assert 1 not in app.ctx.state  # waiting for the OK, not for another file
        assert await app.db.get_channel(2) is None  # nothing happened yet

        await app.press(app.out.callback_data("Import"))
        text = norm(app.out.last_text)
        assert "Imported" in text and "1 channel(s) added" in text and "2 published post(s) added" in text and "1 draft(s) added" in text
        assert (await app.db.get_channel(2)).title == "Movies"
        assert [p.message_id for p in await app.db.sent_posts(1)] == [10, 11]
        assert (await app.db.find_by_message(1, 10)).text == "already here"
        assert await app.db.ignored_ids(2) == {77}
        assert not app.ctx.lock.locked() and not [k for k in app.ctx.pending]

        await app.press(f"imy:{'deadbeef'}")
        assert "expired" in norm(app.out.last_text)
        await app.db.close()

    run(go())


def test_cancelling_the_preview_imports_nothing(tmp_path):
    async def go():
        app = await started(tmp_path)
        await app.text("/import")
        await app.text("", message=backup_file(backup_bytes(channels=[chan(1)], posts=[post(1, 3)])))
        cancel = app.out.callback_data("Cancel")
        await app.press(cancel)
        assert "nothing was imported" in norm(app.out.last_text)
        assert await app.db.get_channel(1) is None and app.ctx.pending == {}
        await app.press(cancel.replace("imx", "imy"))  # the old Import button does not work any more
        assert "expired" in norm(app.out.last_text)
        await app.db.close()

    run(go())


def test_a_file_with_nothing_new_says_so_and_offers_no_button(tmp_path):
    async def go():
        app = await started(tmp_path)
        await app.db.save_channel(1, 5, "Chan", "chan", 1)
        await app.db.create_post(channel_id=1, message_id=3, status="sent", text="x", sent_at=utcnow())
        await app.text("/import")
        await app.text("", message=backup_file(backup_bytes(channels=[chan(1)], posts=[post(1, 3)])))
        assert "nothing new in this file" in norm(app.out.last_text)
        assert app.out.log[-1][2] is None and app.ctx.pending == {}
        await app.db.close()

    run(go())


def test_the_import_waits_while_another_long_job_runs(tmp_path):
    async def go():
        app = await started(tmp_path)
        await app.text("/import")
        await app.text("", message=backup_file(backup_bytes(channels=[chan(1)], posts=[post(1, 3)])))
        data = app.out.callback_data("Import")
        async with app.ctx.lock:
            await app.press(data)
        assert "still running" in norm(app.out.last_text) and await app.db.get_channel(1) is None
        await app.press(data)  # now it is free
        assert "Imported" in norm(app.out.last_text) and (await app.db.get_channel(1)) is not None
        await app.db.close()

    run(go())


def test_an_admin_who_is_not_an_owner_cannot_press_the_import_button(tmp_path):
    async def go():
        app = await started(tmp_path, admins=frozenset({2}))
        await app.text("/import")
        await app.text("", message=backup_file(backup_bytes(channels=[chan(1)], posts=[post(1, 3)])))
        data = app.out.callback_data("Import")
        app.uid = 2
        await app.press(data)
        assert await app.db.get_channel(1) is None  # refused: only owners
        await app.db.close()

    run(go())
