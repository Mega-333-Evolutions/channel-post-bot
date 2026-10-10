# Channel Post Bot

A Telegram bot (Telethon, bot-token login) for creating and managing channel posts with link buttons,
modelled on Channel Help, plus extras for people who run many channels: episode-range auto-suggest and `/autopost`,
`/replace` and `/handleswap`, `/repost` and `/shift` (copy a channel in order; links between posts - and links to them in
your other channels - follow the copies), `/broadcast` (with an optional self-delete time), automatic removal of
Telegram's service notices, a sync that records what other admins do in the channels (and reads in the posts that were
there before the bot), and `/export` / `/import` for backups. Data lives in PostgreSQL (SQLite for local use). Errors are
reported to a Telegram chat.

## What it does

| Area | Features |
|---|---|
| Posts | create text / photo / video / GIF / audio / file posts, formatting kept, preview, publish, drafts |
| Edit later | text or caption, replace media, add / delete buttons, change button text, change button link, reorder buttons (⬅️⬆️⬇️➡️), link-preview toggle, **Mass replace links** (new links for the buttons of a channel, one after the other) |
| Choosing a channel | every command that needs one of your channels lists them as a numbered text list (A to Z, 20 to a message); the buttons only carry the numbers (5 to a row), ◀ ▶ move to the next / previous 20 |
| My posts | `/posts` lists drafts and published posts per channel, edit or delete them. **⬅️ Prev** is always shown; on the first page it jumps to the last page. Owners also get **🔧 Check buttons** (see below) |
| Auto-suggest | `/new` offers the next batch: after "Download Episodes 01 to 20" it proposes "Download Episodes 21 to 40", then 41 to 60 ... Tap **Use suggestion** and only the button link is asked |
| `/autopost` | `/autopost 114 20` posts "Episodes 01 to 20", "21 to 40" ... up to 114, each with a download button, then asks for the real link of every button |
| `/replace` | swaps `@old` → `@new` inside t.me links (button links, hyperlinks, typed links) in **every post of every channel**, also posts not made by this bot. Preview first, full undo. Works only where Telegram lets the bot edit the post |
| `/handleswap` | swaps a `@username` that is *written* in posts (text, captions, button names) - links are left alone. Preview first, full undo |
| `/repost` | copies **every post of a channel, in order**, to the end of the channel (links swapped on the way, replies, pins and links between posts kept), registers the copies in My posts, verifies every button, **points links to the old posts - in this and in all your other connected channels - at the copies**, then deletes the old posts after your confirmation |
| `/shift` | copies posts from **one channel into another** (all, or a range of message ids); the source is only read. `-c` points links to the source's posts in all your connected channels at the copies, `-all` also gives the destination the source's name, description and photo. Posts Telegram holds back after a copyright strike are copied from My posts |
| `/broadcast` | sends one message to every channel; with a time (`50m`, `1h`, `5d`) it is deleted again from the channels and from My posts. `/boardcast` works too |
| Service notices | "name changed", "photo changed", "pinned a message", live stream notices ... are deleted from every connected channel |
| Sync | posts deleted, edited or added by other admins are recorded in My posts (`/sync` runs it now); the posts that were in a channel before the bot was added are read in; what Telegram itself does to a channel (a copyright strike) is never taken for an edit |
| Backup | `/export` downloads a JSON backup of your channels and posts, `/import` puts it back (it only adds) |
| Userbot (optional) | a helper user account that deletes the old posts the bot itself is not allowed to delete (older than 48 hours) |
| Error log | every error goes to a Telegram chat as a Python code block with the full traceback |

## Setup (local)

1. Create a bot with @BotFather and copy the token.
2. Get `API_ID` and `API_HASH` at https://my.telegram.org (API development tools).
3. Database: create a free Postgres (for example at neon.tech) and copy its connection string into `DATABASE_URL`.
   Leave it empty to use a local SQLite file.
4. `cp .env.example .env`, fill it in (`OWNER_IDS` = your numeric Telegram id, ask @userinfobot).
5. `pip install -r requirements.txt` then `python bot.py`.
6. Add the bot to each channel as **admin** with: *Post messages*, *Edit messages of others*, *Delete messages*
   (*Edit messages of others* is also what lets it pin; *Delete messages* is what removes the service notices).
   For the userbot clean-up below it also needs *Invite users via link* and *Add new admins*.
7. In the bot: `/addchannel`, then forward a post from the channel (or send its @username).
   Private channel? Post something in the channel while the bot is running, then forward that post.
8. Add the bot to the error-log chat (see **Error log** below).

New tables (`channel_sync`, `channel_history`, `ignored_posts`, `job_marks`) are created by the bot itself when it starts;
no existing table or column changes, so nothing has to be migrated by hand.

## Settings

All settings are environment variables (a `.env` file works locally; on a host use its secrets / variables page).

| Name | Meaning |
|---|---|
| `API_ID`, `API_HASH`, `BOT_TOKEN` | Telegram credentials (required) |
| `OWNER_IDS` | your numeric Telegram id(s), comma separated (required). Owners can do everything |
| `ADMIN_IDS` | ids that may create / edit posts but not use the owner tools |
| `DATABASE_URL` | PostgreSQL URL (Neon works), empty = local SQLite |
| `DB_POOL` | `null` (default, short connections, lets serverless Postgres sleep) or `queue` |
| `ERROR_LOG_CHAT_ID` | chat that receives the error reports. Empty = `-1002525172451`, `0` or `off` = switched off |
| `USERBOT_SESSION` | session text of the helper account (see **Userbot**). Empty = no userbot |
| `USERBOT_KEEP_ADMIN` | `true` = the userbot stays an admin after a clean-up (default: the right is taken away again) |
| `DELETE_SERVICE_MESSAGES` | `true` (default) = the bot deletes the notices Telegram adds to the channels. `false` = leave them |
| `IMPORT_OLD_POSTS` | `true` (default) = the posts that were in a channel before the bot was added are read into My posts too (see **Sync**). `false` = follow only what happens from now on |
| `SYNC_INTERVAL_MINUTES` | minutes between two full checks of every channel against My posts (default 60). `0` = no automatic checks and no live tracking of edits / deletes / new posts; `/sync` still works. Every check wakes a serverless database such as Neon for a few minutes, so a longer value (e.g. `180`) lets it sleep more |
| `SESSION_PATH` | where the bot keeps its login. Empty = a temporary folder outside the app folder (on purpose) |
| `SESSION_STRING` | optional, from `python make_session.py` |
| `EDIT_DELAY` | seconds between edits / copies (default 1.2, minimum 0.3) |
| `REPLACE_TYPED_LINKS` | also swap links typed as plain text (default true) |
| `PORT` | the bot answers `ok` on this port (not needed with the Docker setup below) |
| `LOG_LEVEL` | `INFO` (default) |

## Hugging Face Space + GitHub (Docker)

The `Dockerfile` is the one you supplied: it installs the requirements, starts `python -m http.server 7860` in the
background (so the Space has something listening on its port) and runs `python bot.py`.
`.github/workflows/hf-sync.yml` (also yours) copies the repository into the Space `Mega-Evolutions/ButtonBot`
on every push to `main`, leaving the Space's own `README.md` alone.

- Put **all** settings into the Space's *Settings → Variables and secrets* as secrets. The GitHub repository
  needs one secret, `HF_TOKEN` (a Hugging Face token with write access).
- `python -m http.server` publishes every file in `/app` to anyone who knows the Space address. So nothing secret
  may be inside the image: the repository must not contain `.env`, `*.session` or database files (`.gitignore` and
  `.dockerignore` keep them out), and the bot keeps its login in the temporary folder, not in `/app`.
  Do not set `PORT=7860` as well; the bot would have nothing to add and just logs that the port is taken.
- The temporary folder is wiped on every restart. That costs nothing: a bot logs in again with its token.
- Free Spaces can sleep when nobody visits them. A sleeping Space does not run the bot: nothing is checked, no timed
  broadcast is deleted and no notice is removed while it sleeps. What is overdue is done after it wakes up.

## `/autopost` and Mass replace links

`/autopost 114 20` (total episodes, interval) asks which channels to post in, then posts one message per range:
`Episodes 01 to 20`, `21 to 40` ... each with a **Download Episodes 01 to 20** button that first points at a
placeholder link. A short last range gets its own post when it is longer than half an interval (114 / 20 → `101 to 114`)
and is added to the post before it when it is half an interval or less (102 / 20 → `81 to 102`). At most 200 posts per run.
Afterwards the bot asks for the real link of the first post's button, then of the next, and so on; every answer changes
the button in the channel at once. The posts are in My posts like any other.

**Mass replace links** is the same question-after-question flow for a channel you already have: open a published button
post in `/posts` and tap it; the bot asks for that post's new link, then for the next button post of the channel (posts
without a link button are skipped). Stop with ✖️ Cancel at any time; what was answered stays changed.

## `/replace` and `/handleswap`

`/replace @old @new` shows what would change, nothing is edited until you press **Apply**. Try it small first:
`/replace @old @new --last 50`, look at a changed post, `/undo` if it is wrong, then run it for everything.
If Telegram refuses to edit posts made by another bot (the preview and the result say so), use `/repost`.

Options: `--channel @name` · `--last N` (newest N post ids) · `--posts` (also change `t.me/old/123` post links; skipped
by default) · `--no-typed` (leave links typed in the text alone). Only the username changes:
`https://t.me/old?start=XYZ` → `https://t.me/new?start=XYZ`. Other bots' buttons (reactions) are kept as they are.
Every edited post is saved in the database ("adopted"), so it shows up in `/posts`.

`/handleswap @bro @sis` is the other half: it changes the username when it is **written** in a post - in the text or caption
and in button names - and leaves every link, hyperlink and button link as it is. Same preview, same options
(`--channel`, `--last`), same `/undo`. Only usernames are accepted, not links.

## `/repost`: rebuild a channel in order

Use it when Telegram won't let the bot edit another bot's buttons. The copies are posted by this bot, so from then on
every post and button is editable here.

1. `/repost @old @new` (leave the usernames out to copy without changing links - a bare `/repost` only shows how it
   works, so add an option such as `--last 3` or `--no-typed`). **Pick the channel from the numbered list** that
   appears: press the number of its name. There is no `--channel` option any more.
2. The bot reads the channel and shows a plan: how many posts, which links change, what is not kept.
   Trial first: `/repost @old @new --last 3` copies only the newest 3 post ids; look at them at the end of the channel,
   then tap **Undo** (removes just the copies).
3. **Start copying.** One post per ~1-2 s, **Stop** and **Continue** work without copying anything twice. Don't post
   in the channel meanwhile.
4. Look at the end of the channel (formatting, buttons, media, albums, replies, pins).
5. **Delete the old posts** (asks again) or **Undo**. Old posts are only offered for deletion after a complete copy,
   and only the ones that have a copy are deleted.

How it copies:

- Posts that need no change (no buttons, no link to swap, not an answer to another post) are copied by Telegram itself:
  no "forwarded from" header, formatting, quotes, spoilers, media and albums stay exactly the same.
- Posts with buttons, with links to swap, or that answer another post are posted again in **one request** with their final
  text, entities, media and buttons. The buttons are never added in a second step.
- **Replies are kept:** a post that answered post 12 now answers the *copy* of post 12.
- **Links between posts are kept:** a link such as `https://t.me/channel/115` - as a hyperlink behind text, typed in the
  text, or on a button - is pointed at the copy of post 115. Only the digits change; `?single`, `?comment=3` and the
  visible text stay. A link to a post that is copied later is fixed at the end.
- **Pins are kept:** the copy of a pinned post is pinned (silently) and Telegram's "pinned a message" notice is deleted
  again. Pinning needs the *Edit messages of others* right; if the bot lacks it, the result says which copies are not pinned.
- **Links to the old posts are pointed at the copies in every connected channel.** When all posts are copied the bot reads
  your connected channels (message ids 1 up to the channel's counter, so a big channel takes a while), finds every link to
  an old post - a hyperlink behind text, a link typed in the text, a link button; the `t.me/name/52` and the
  `t.me/c/123/52` form - and points it at the copy. Only the digits change: the text around the link, the formatting,
  `?single` and all other buttons stay. A link to a post that has no copy stays as it is, and running it again changes
  nothing twice. A post made by somebody else is edited only if the bot has the *Edit messages of others* right (otherwise
  it is counted in the report); posts Telegram holds back and channels Telegram restricts are not edited; nothing is deleted.
  **🔗 Update links again** repeats it (for links added since), and **Undo** points the links back at the original posts
  before it removes the copies.
- **A post Telegram holds back is not copied** (see **Copyright strikes** below): copying the notice would be wrong. It is
  listed in the result, the old posts of such a repost are not offered for deletion, and nothing is lost.
- **Every new post is read back from the channel.** If its buttons are not shown, the bot repairs it (a second edit,
  then a two-step edit). If they still do not show, that copy is removed again and the post is reported as
  `ButtonsNotShown`; the original stays untouched and **Try the rest again** retries it.
- If the channel has *Restrict saving content* switched on, forwarding is blocked and the bot rebuilds every post
  from its parts.
- Link buttons are copied with the new username; other bots' buttons (reactions) are not copied because they would be dead.
- Not kept, because Telegram can't: original dates, view counts, reactions, comments, poll votes. Copies are sent silently.

### Deleting the old posts

Telegram's Bot API documentation says bots can delete only messages younger than 48 hours. This bot talks to
Telegram directly and I could not verify whether that limit applies in channels. So: the bot always tries first and
**checks by reading the messages back**. Whatever is still there goes to the userbot (if you set one up).
Without a userbot, the bot tells you what is left and which messages they are; delete those by hand and tap
**Close this repost**.

## `/shift`: copy posts from one channel into another

`/shift @source @destination` copies every post of the source into the destination, in order, with text, formatting,
media, albums, buttons and replies, and lists the copies in My posts. `/shift @source @destination 15 20` copies only the
posts with message ids 15 to 20, `/shift @source @destination 15` only post 15.

Two optional extras can be added - alone or together, in any order, before or after the range (`- all` typed with a space
is understood too):

| Option | What it does |
|---|---|
| `-c` | afterwards the bot looks through **all your connected channels** for links to a post of the source - `https://t.me/old/52`, `https://t.me/c/123/52`, a hyperlink behind text, a link typed in the text, a link button - and points each at the equivalent post of the destination (`https://t.me/new/<id of its copy>`). The source itself is only read |
| `-all` | afterwards the destination gets the **name, description and profile photo** of the source. Its own are replaced (undo does not bring them back); a part the source does not have is left as it is. The bot needs the *Change channel info* admin right in the destination (the plan warns if it lacks it) |

`/shift @old @new`, `/shift @old @new -c`, `/shift @old @new -all`, `/shift @old @new -c -all` and
`/shift @old @new -all -c` all work; so does a range: `/shift @old @new 15 20 -c`.

- The source is **only read**: nothing in it is changed or deleted, and the bot need not be an admin there. It does have to
  be able to read it: a member of it, or a public channel found by its `@username`. Give it as `@username`, a t.me link,
  the id (`-100…`, only for channels the bot is part of) or an invite link (that only tells the bot which channel you
  mean - it cannot join by itself). The bot must be an admin of the destination (*Post messages*).
- You see a plan first and press **Start copying**. **Stop** and **Continue** work like in `/repost`; **Undo** removes the
  copies again from the destination. The extras are remembered with the shift, so **Continue** and **Try the rest again**
  keep them.
- Links from a copied post to another post of the source follow the content: if that post was copied too, the link points
  at its copy in the destination; links to posts that were not copied stay as they were.
- **`-c`** follows the same rules as the repost's link update (see above): only links to posts that were copied change, a
  second run changes nothing twice, posts of others are edited only with the *Edit messages of others* right, nothing Telegram
  holds back is edited. The **source is left out of the search** - it is only read - the destination's own older posts are
  included. **🔗 Update links again** repeats it; **Undo** points the links back at the source's posts first (links in the
  private `t.me/c/…` form come back in the public `t.me/name/…` form of the same post).
- **`-all`** copies the three parts one by one: if one fails (for instance the missing right) the others still happen, and
  the result names what failed and offers **🖼 Copy name, description, photo again**. Telegram's "channel name / photo
  changed" notices that this makes are deleted again.
- Forwarding is used where the source allows it (no "forwarded from" header); a source with *Restrict saving content*
  is rebuilt from its parts.
- **A source Telegram restricts** (a copyright strike) is copied from My posts - see the next section.

## Copyright strikes: what Telegram does is not an admin's edit

When a channel gets a copyright strike, Telegram shows "This message couldn't be displayed on your device due to copyright
infringement." in place of its posts. The bot treats that as what it is - something Telegram did, not something an admin did:

- **Sync:** My posts stays exactly as it was saved. A channel Telegram restricts is not looked at (the report says so), a single
  post Telegram holds back, a notice shown in place of a post, and many posts that suddenly all show one and the same text are
  never taken for edits: nothing is edited, removed or added for them.
- **`/shift`:** a post of the source that Telegram holds back is copied from what **My posts** saved of it - text, formatting,
  buttons and media (that is why My posts should have the channel's posts *before* the strike: connect the channel early, or
  `/import` a backup). The plan counts them. A saved media file that Telegram refuses (a file taken down for copyright) is
  tried again without its old file reference and, if it is still refused, a post that has text goes out without the file
  (the result names these posts; a post that is only a refused file fails and is listed). A post that My posts has no saved
  copy of is skipped and listed, and the shift then shows "not everything was copied": after the data is there - for
  instance after `/import` - **Try the rest again** copies the rest. If the source **can't be read at all** (a banned or
  private channel) and My posts has its posts, the plan says so and offers to copy everything from My posts alone; replies,
  pins and albums are not kept that way (an album's pictures become posts of their own).
- **`/repost`:** a post Telegram holds back is not copied (the notice must not become a post) and so is not deleted.
- **Links (`-c`, repost):** posts Telegram holds back are not edited.

A post counts as held back when what Telegram hands out for it is its notice - or nothing - instead of the post. A post that
carries Telegram's restriction mark but is still handed out as it is, is an ordinary post for a copy.

## 📣 `/broadcast`

`/broadcast` (or `/boardcast`) sends one message to every connected channel; owners only. Then you send the message
(text, or a photo / video / GIF / file with a caption; formatting is kept), optionally buttons, look at the preview and
confirm. Every copy is saved in My posts.

`/broadcast 1h` (also `50m`, `5d`, `1d12h`; from 1 minute to 365 days) deletes the message from all the channels and from
My posts when the time is up. The schedule is kept in the database: a restart loses nothing, and whatever is overdue is
deleted when the bot runs again. If a deletion fails for good (the bot was removed from the channel ...) you are told.
A broadcast copied later with `/repost` or `/shift` becomes an ordinary post: its timer does not travel with it.

## 🔧 Check buttons (My posts)

`/posts` → pick a channel → **🔧 Check buttons** (owners only). It compares the buttons saved in the bot with what the
channel really shows for every published post that has link buttons, and lists the posts that are saved with buttons
but show none (or other links). **Fix** puts the saved buttons back. Use it once for channels you reposted with the
earlier version, where some posts showed their buttons in My posts but not in the channel.

## Service notices

When a channel's name or photo changes, something is pinned, a video chat or live stream starts or ends ... Telegram adds a
small notice to the channel. The bot deletes every one of them in every connected channel, as soon as it appears
(`DELETE_SERVICE_MESSAGES=false` switches that off). It needs the *Delete messages* right; if it lacks it you get one message per
channel per day saying so.

- Telegram's documentation says bots may delete only messages younger than 48 hours (see **Deleting the old posts** for
  what is not verified), and the notice that says a channel was created usually can't be deleted at all. Notices that were
  already there when you added the bot are found while the sync reads the channel's older posts (see **Sync**); if you set up
  the userbot, the helper account deletes the ones the bot may not (see **Userbot**).
- The notices that a `/repost` or `/shift` pin makes are removed right after pinning, as before.

## Sync: what other admins do in the channels

The bot compares the channels with My posts, so My posts shows what is really in the channel even when someone else
touches it:

- a post that was **deleted** in the channel is removed from My posts, with everything saved of it (a timed deletion that is
  not needed any more goes too);
- a post that was **edited** in the channel (text, formatting, buttons, media) gets its saved copy updated;
- a **new post** that was not made with this bot is added to My posts (it says "Picked up from the channel");
- the posts that were in the channel **before the bot was added** are read into My posts too, oldest first, up to the newest
  message that existed at the first look. Where the reading stopped is saved: a timed check reads for about two and a half
  minutes per channel and goes on next time, `/sync` reads on until it is done. `IMPORT_OLD_POSTS=false` switches this off.

This runs in three ways: **live** (a few seconds after Telegram tells the bot about a change), **on a timer** (every
`SYNC_INTERVAL_MINUTES`, default 60) and **on demand** with `/sync` (owners; `/sync --channel @name` for one channel,
`/sync --history` reads the channel's older posts again and adds the ones that are missing). `/sync` answers with what it
found, channel by channel. Automatic runs are silent. While a long job (`/repost`, `/shift`, `/replace`, `/broadcast` ...)
runs, the automatic runs wait; `/sync` refuses.

Rules that keep it safe:

- **What Telegram does is not an edit.** A channel Telegram restricts, a post it holds back, a notice shown in place of a
  post (the copyright notice) and many posts that suddenly show one and the same text are left exactly as saved - see
  **Copyright strikes**.
- A post the bot touched a moment ago, and a message that appeared a moment ago (two minutes for the timed check, ten
  seconds for live changes), are judged at the next look: the bot may still be saving it.
- If a saved post shows **no buttons** in the channel, the saved buttons are **kept** (Telegram sometimes hides a keyboard);
  the report says so and **🔧 Check buttons** can put them back.
- If **every** saved post of a channel looks deleted (and there are five or more), nothing is removed - more likely the bot
  lost access. The report says so.
- Posts you forget with "Only forget it in the bot" are not picked up again, not even by the older-posts reading. Polls,
  stickers and other kinds My posts can't hold are left alone and only counted.
- The older posts are found by reading message ids one range after the other (Telegram has no list of a channel's
  posts for a bot), so a channel with hundreds of thousands of ids takes a while the first time.
- A quick look reads the ids that can plausibly be new. If more than about 300 message ids were deleted in a row right
  before a new post, that post may wait for the daily full look or for `/sync`.

## `/export` and `/import`: backup and restore

`/export` (owners) sends a JSON file with your channels, every post saved in My posts (published and drafts) and the posts
you made the bot forget. Keep it somewhere safe: it is also what lets a channel that was struck or lost be copied again
(see **Copyright strikes**).

`/import` asks for that file as your next message, reads it as data only (every value is checked), and shows what would be
added: channels, published posts, drafts. Nothing happens until you press **✅ Import**. Importing **only adds**:

- a channel the bot already has is left as it is (its name, username and access come from Telegram, which is newer than the
  file); a channel it does not have is added;
- a post at a message id the bot already has is left as it is; every other published post is added, and so is every draft that
  is not here yet;
- a post you made the bot forget is not brought back;
- nothing is ever changed or deleted. Importing twice adds nothing the second time.

Imported channels and posts are rows in the bot's own database: to post or edit in a channel the bot still has to be an admin there (on a different bot, add the channel again with `/addchannel`).

## Userbot (optional): deleting posts older than 48 hours

1. On **your own computer**: `pip install telethon`, `python make_userbot_session.py`. Log in with a **separate**
   Telegram account (not your main one). It prints `USERBOT_SESSION=...`.
2. Put that text into the Space / host secrets as `USERBOT_SESSION`. Whoever has it can act as that account; you can end
   it any time in Telegram → Settings → Devices.
3. In the bot, `/userbot` shows whether it is connected.

What happens when the bot cannot delete something (nothing happens before that, and only for that channel):

1. The bot makes a one-time invite link (1 hour, 1 use, titled "Cleanup helper"); if it may not, it uses the channel's
   own invite link; for a public channel the userbot joins by its @username.
2. The userbot joins. A channel that makes new members wait for approval needs you to approve the request once.
3. If the userbot may not delete there and the bot has the *Add new admins* right, the bot makes it an admin with
   only **Delete messages**. Otherwise the message tells you which right to give, or add the userbot yourself.
4. The userbot deletes (100 per request) and the bot checks by reading back.
5. Afterwards the bot takes the admin right away again (unless `USERBOT_KEEP_ADMIN=true`) and revokes the temporary
   link. The account stays a member of the channel; leave with it by hand if you like.

The result message lists these steps, so you can see exactly what was done.

## Error log

Every error the bot meets is sent to `ERROR_LOG_CHAT_ID` as a Telegram code block (`python`), like the One Piece
Bounty Bot: `Unhandled exception in background task <unnamed>:` followed by the whole traceback, including
*The above exception was the direct cause of the following exception*. Reported are: errors in handlers, log
records of level ERROR, exceptions nobody caught in background tasks, and a crash of the whole bot (sent right before it stops).

- The bot must be in that chat (member of a group, or admin of a channel with *Post messages*). At start-up the log
  says whether it can reach the chat.
- Long tracebacks are split into several messages `(part 1/3)`, never shortened.
- The bot token, API hash, session texts and the database password are replaced by `***` before anything is sent.
- The same error within a minute is sent once with a counter; at most 12 reports per minute; if the chat refuses the
  bot, reporting pauses for 10 minutes.

## Commands

`/new` `/posts` `/autopost` `/cancel` · `/addchannel` `/channels` · owner only: `/broadcast` (`/boardcast`) `/replace`
`/handleswap` `/undo` `/repost` `/shift` `/sync` `/userbot` `/export` `/import`

Auto-suggest reads the newest published post of the channel that has a button with numbers in its text
(`Episodes 01 to 20`, `1-12`, `S01E01-E12`, `Episode 05`). It keeps zero-padding and batch size, advances the
same numbers in the post text, carries over the media and asks for new links. It is computed from your
posts, so cancelled drafts don't skip numbers and editing or deleting the last post updates it.

## Good to know

- **Not tested against real Telegram.** The tests use a simulated channel, build and serialise every request with the
  real Telethon, and run on SQLite and on PostgreSQL 16. What the simulation cannot show: whether Telegram delivers every
  channel update to the bot (the live sync and the notice removal depend on it - the timed check is the safety net), whether
  a bot may pin and delete as assumed, how forwarding behaves for every kind of source, and the real 48-hour delete limit.
  The same goes for the copyright rules: how exactly Telegram marks a held-back post (its own restriction mark, or the
  notice as the text) is not verified - the bot recognises both, but the first real strike may show another shape. Try
  `/repost --last 3` and `/shift` with a small range on a test channel first.
- `/shift -all` copies the name, description and profile photo only - not the invite links, the username, the admins or the
  channel's settings - and undo does not give the destination its old ones back. `-c` and the repost's link update read every
  message id of every connected channel up to its counter, so a big channel takes a while; a post of somebody else is only
  edited with the *Edit messages of others* right.
- The live parts (notices, sync, timed broadcasts) only run while the bot runs. After a sleep or a restart the next timed
  check (about 90 seconds after start) catches up, and `/sync` can be used by hand at any time.
- About "buttons missing in the channel although My posts shows them": the cause is not confirmed. The most likely one
  is that a copy made by Telegram's own copy function keeps a keyboard Telegram then treats as "already set", so the
  follow-up edit with the same keyboard was answered "not modified" and ignored. That is why button posts are now
  created with their buttons in one request and read back, and why **Check buttons** exists. If it ever still happens,
  the result says `ButtonsNotShown` instead of staying silent.
- Albums can't carry buttons, so a post is one text or one media message.
- Captions: 1024 characters; text posts: 4096.
- A published media post can't be turned back into plain text (Telegram limitation).
- Saved drafts store Telegram's media reference. If it has expired the bot retries without it; if Telegram
  still refuses, send the media again with 🖼 Media.
- `/replace` and `/handleswap` read posts by id, from 1 up to the channel's counter. Big channels take a while; edits are
  spaced by `EDIT_DELAY` seconds to stay inside Telegram's limits.
- Telethon is pinned to 1.45.x: that version changed how inline buttons are represented.

## Tests

`pip install -r requirements-dev.txt && pytest` (SQLite). For PostgreSQL:
`TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/db pytest`.
