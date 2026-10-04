"""Settings from the environment, the deployment files, and that nothing the owner needs is left undocumented."""
import asyncio
import py_compile
import re
import socket
import tempfile
from pathlib import Path

import pytest

from app.config import DEFAULT_ERROR_LOG_CHAT_ID, Config, default_session_path, load_config
from app.health import start_health_server
from bot import COMMANDS, secrets_of

from .harness import App

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = {"API_ID": "12345", "API_HASH": "abcdef0123456789", "BOT_TOKEN": "123456:ABCdefGHIjklMNOpqrSTUvwxYZ", "OWNER_IDS": "1001484109"}


def env_names() -> list:
    """Every environment variable app/config.py reads."""
    src = (ROOT / "app" / "config.py").read_text()
    names = set(re.findall(r'(?:getenv|_ints|_bool|_chat_id|_float)\(\s*"([A-Z][A-Z0-9_]+)"', src))
    names |= set(re.findall(r'environ\[\s*"([A-Z][A-Z0-9_]+)"', src))
    return sorted(names)


@pytest.fixture
def env(monkeypatch):
    for k in env_names():
        monkeypatch.delenv(k, raising=False)
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    return monkeypatch


# ----------------------------------------------------------------------------------------- defaults
def test_the_names_the_test_looks_for_are_all_found():
    names = env_names()
    for expected in ("API_ID", "BOT_TOKEN", "OWNER_IDS", "ERROR_LOG_CHAT_ID", "USERBOT_SESSION", "USERBOT_KEEP_ADMIN", "SESSION_PATH", "PORT"):
        assert expected in names, names


def test_defaults(env):
    cfg = load_config()
    assert cfg.error_log_chat_id == DEFAULT_ERROR_LOG_CHAT_ID == -1002525172451
    assert cfg.userbot_session == "" and cfg.userbot_keep_admin is False
    assert cfg.owners == frozenset({1001484109}) and cfg.admins == frozenset()
    assert cfg.edit_delay == 1.2 and cfg.replace_typed_links is True and cfg.port == 0
    assert cfg.database_url.startswith("sqlite")


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, -1002525172451),  # not set at all -> the default chat
        ("", -1002525172451),
        ("   ", -1002525172451),
        ("-1001234567890", -1001234567890),
        (" -1001234567890 ", -1001234567890),
        ("123456789", 123456789),  # a private chat with a person
        ("0", None),
        ("off", None),
        ("OFF", None),
        ("None", None),
        ("false", None),
        ("no", None),
        ("disabled", None),
    ],
)
def test_error_log_chat_id(env, raw, expected):
    if raw is not None:
        env.setenv("ERROR_LOG_CHAT_ID", raw)
    assert load_config().error_log_chat_id == expected


@pytest.mark.parametrize("raw", ["abc", "-100 123", "@mychannel", "1.5"])
def test_a_bad_error_log_chat_id_is_refused_with_its_name(env, raw):
    env.setenv("ERROR_LOG_CHAT_ID", raw)
    with pytest.raises(SystemExit) as e:
        load_config()
    assert "ERROR_LOG_CHAT_ID" in str(e.value) and repr(raw) in str(e.value)


def test_userbot_settings(env):
    env.setenv("USERBOT_SESSION", "  1BVtsOKwBu7abc  ")
    env.setenv("USERBOT_KEEP_ADMIN", "true")
    cfg = load_config()
    assert cfg.userbot_session == "1BVtsOKwBu7abc" and cfg.userbot_keep_admin is True
    for v, want in [("1", True), ("yes", True), ("ON", True), ("false", False), ("0", False), ("", False), ("nonsense", False)]:
        env.setenv("USERBOT_KEEP_ADMIN", v)
        assert load_config().userbot_keep_admin is want, v


def test_missing_settings_are_listed(env):
    env.delenv("API_HASH")
    env.delenv("OWNER_IDS")
    with pytest.raises(SystemExit) as e:
        load_config()
    assert "API_HASH" in str(e.value) and "OWNER_IDS" in str(e.value) and "API_ID" not in str(e.value)
    env.setenv("OWNER_IDS", "not a number")
    env.setenv("API_HASH", "x")
    with pytest.raises(SystemExit) as e:
        load_config()
    assert "OWNER_IDS" in str(e.value)


def test_api_id_must_be_a_number(env):
    env.setenv("API_ID", "twelve")
    with pytest.raises(SystemExit) as e:
        load_config()
    assert "API_ID must be a number" in str(e.value)


def test_edit_delay_has_a_floor(env):
    env.setenv("EDIT_DELAY", "0.1")
    assert load_config().edit_delay == 0.3
    env.setenv("EDIT_DELAY", "banana")
    assert load_config().edit_delay == 1.2
    env.setenv("EDIT_DELAY", "2.5")
    assert load_config().edit_delay == 2.5


def test_the_session_is_not_kept_in_the_app_folder_by_default(env):
    path = Path(load_config().session_path).resolve()
    assert str(path).startswith(str(Path(tempfile.gettempdir()).resolve()))
    assert ROOT not in path.parents  # an app folder that a web server publishes must not contain the login
    assert Path(default_session_path()).name == "bot"
    env.setenv("SESSION_PATH", "data/bot")
    assert load_config().session_path == "data/bot"  # still possible when somebody wants it


# ------------------------------------------------------------------------------ secrets of the bot
def test_every_secret_is_collected_for_redaction():
    cfg = Config(
        api_id=1, api_hash="HASHHASHHASH", bot_token="123:TOKEN-TOKEN", owners=frozenset({1}), admins=frozenset(),
        database_url="postgresql://user:p%40ss%2Fword@host.example/db?sslmode=require", db_pool="null", session_path="x",
        session_string="SESSIONSTRINGTEXT", edit_delay=1.0, replace_typed_links=True, port=0, log_level="INFO",
        userbot_session="USERBOTSESSIONTEXT",
    )
    found = secrets_of(cfg)
    for s in ("HASHHASHHASH", "123:TOKEN-TOKEN", "SESSIONSTRINGTEXT", "USERBOTSESSIONTEXT", "p%40ss%2Fword", "p@ss/word"):
        assert s in found, (s, found)
    assert "" not in found
    plain = Config(**{**cfg.__dict__, "database_url": "sqlite+aiosqlite:///data/bot.db", "session_string": "", "userbot_session": ""})
    assert set(secrets_of(plain)) == {"123:TOKEN-TOKEN", "HASHHASHHASH"}  # nothing else to hide, and no crash on SQLite


# ------------------------------------------------------------------------------------- deployment files
def test_dockerfile_is_exactly_the_one_that_was_asked_for():
    lines = [ln.rstrip() for ln in (ROOT / "Dockerfile").read_text().splitlines() if ln.strip()]
    assert lines == [
        "FROM python:3.12-slim",
        "WORKDIR /app",
        "ENV PYTHONUNBUFFERED=1",
        "COPY requirements.txt .",
        "RUN pip install --no-cache-dir -r requirements.txt",
        "COPY . .",
        "CMD python -m http.server 7860 & python bot.py",
    ]


def test_github_workflow_syncs_to_the_space_without_a_hard_coded_token():
    text = (ROOT / ".github" / "workflows" / "hf-sync.yml").read_text()
    assert "huggingface.co/spaces/Mega-Evolutions/ButtonBot" in text
    assert "${{ secrets.HF_TOKEN }}" in text and "branches: [main]" in text and "workflow_dispatch" in text
    assert "--exclude='README.md'" in text and "--exclude='.github'" in text  # the Space keeps its own README
    assert not re.search(r"hf_[A-Za-z0-9]{20,}", text)  # no real token pasted in
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(text)
    assert doc["jobs"]["sync-to-hub"]["steps"][1]["env"]["HF_TOKEN"] == "${{ secrets.HF_TOKEN }}"


def test_nothing_secret_can_end_up_in_the_image_or_the_repository():
    docker = (ROOT / ".dockerignore").read_text().split()
    for entry in (".env", "*.session", "data/*", "tests"):
        assert entry in docker, entry
    git = (ROOT / ".gitignore").read_text().split()
    for entry in (".env", "*.session"):
        assert entry in git, entry


def test_env_example_documents_every_setting():
    text = (ROOT / ".env.example").read_text()
    listed = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.M))
    missing = [n for n in env_names() if n not in listed]
    assert not missing, f".env.example does not mention: {missing}"
    unknown = sorted(listed - set(env_names()))
    assert not unknown, f".env.example mentions settings the bot does not read: {unknown}"
    assert re.search(r"^ERROR_LOG_CHAT_ID=-1002525172451$", text, re.M)


def test_helper_scripts_compile_and_the_userbot_one_stands_alone():
    for name in ("make_userbot_session.py", "make_session.py", "bot.py"):
        py_compile.compile(str(ROOT / name), doraise=True)
    src = (ROOT / "make_userbot_session.py").read_text()
    assert "from app" not in src and "import app" not in src  # runs on its own computer with only telethon installed
    assert "StringSession" in src and "USERBOT_SESSION=" in src


def test_the_testing_commands_are_gone_everywhere():
    names = [c for c, _ in COMMANDS]
    assert "testedit" not in names and "selftest" not in names
    assert {"new", "posts", "repost", "replace", "undo", "userbot", "testerror", "help"} <= set(names)
    for path in list((ROOT / "app").rglob("*.py")) + [ROOT / "bot.py"]:
        text = path.read_text().lower()
        assert "testedit" not in text and "selftest" not in text, path


def test_every_menu_command_has_a_handler(tmp_path):
    async def go():
        app = App(tmp_path)
        await app.start()
        patterns = [b.pattern for b, _ in app.tg.handlers if getattr(b, "pattern", None)]
        for name, _ in COMMANDS:
            assert any(p(f"/{name}") for p in patterns), f"/{name} is in the menu but nothing answers it"
        await app.db.close()

    asyncio.run(go())


def test_the_help_text_lists_the_commands_that_exist():
    from app.handlers.basic import HELP

    for name in ("new", "posts", "repost", "replace", "undo", "userbot", "testerror", "export", "addchannel", "channels"):
        assert f"/{name}" in HELP, name
    assert "--channel" not in HELP


# ------------------------------------------------------------------------------------------ health port
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_health_endpoint_answers_ok():
    async def go():
        port = free_port()
        server = await start_health_server(port)
        assert server is not None
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), 5)
        assert data.startswith(b"HTTP/1.1 200 OK") and data.endswith(b"ok")
        writer.close()
        server.close()
        await server.wait_closed()

    asyncio.run(go())


def test_a_port_that_is_already_taken_does_not_stop_the_bot():
    """The Dockerfile starts `python -m http.server 7860` first; if PORT=7860 is set too, the bot must carry on."""

    async def go():
        busy = socket.socket()
        busy.bind(("0.0.0.0", 0))
        busy.listen()
        try:
            assert await start_health_server(busy.getsockname()[1]) is None
        finally:
            busy.close()

    asyncio.run(go())

