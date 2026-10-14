"""API keys, kept outside the project so they never land in .orion/ or in a commit.

A key is read from its environment variable first, then from the keys file: $ORION_KEYS_FILE, else
$XDG_CONFIG_HOME/orion/keys.env, else ~/.config/orion/keys.env. The file holds KEY=value lines and is written
readable only by you (file mode 0600, folder 0700). Nothing here prints a key.
"""
import os
import re
import tempfile
from pathlib import Path

NAMES = ("POSTHOG_PERSONAL_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "VERTEX_API_KEY", "TYPESAFE_API_KEY")
ALIASES = {"GEMINI_API_KEY": ("GOOGLE_API_KEY",)}  # Also accepted when reading.
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def keys_file():
    if os.environ.get("ORION_KEYS_FILE"):
        return Path(os.environ["ORION_KEYS_FILE"]).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "orion" / "keys.env"


def parse_line(line):
    """(name, value) for a KEY=value line; (None, None) for blanks and # comments. `export ` and quotes are allowed."""
    text = line.strip()
    if text.startswith("export "):
        text = text[7:].lstrip()
    name, sep, value = text.partition("=")
    name = name.strip()
    if not sep or not NAME.fullmatch(name):
        return None, None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return name, value[1:-1]
    return name, re.split(r"\s+#", value, maxsplit=1)[0]


def read(path=None):
    """The keys file as {name: value}; empty when there is no file."""
    path = Path(path) if path else keys_file()
    if not path.is_file():
        return {}
    pairs = (parse_line(line) for line in path.read_text().splitlines())
    return {name: value for name, value in pairs if name}


def _names(name):
    return (name, *ALIASES.get(name, ()))


def env_name(name):
    """The environment variable that holds this key right now (the name itself or an alias), or None."""
    return next((n for n in _names(name) if os.environ.get(n, "").strip()), None)


def get(name):
    """The key from its environment variable, else from the keys file; None when neither has it."""
    held = env_name(name)
    if held:
        return os.environ[held].strip()
    stored = read()
    return next((stored[n] for n in _names(name) if stored.get(n)), None)


def where(name):
    """Where get() finds the key, in words and without the key itself; None when it is not set."""
    held = env_name(name)
    if held:
        return f"environment variable {held}"
    stored = read()
    return str(keys_file()) if any(stored.get(n) for n in _names(name)) else None


def save(values):
    """Merges keys into the keys file (comments and other keys are kept) and returns its path.

    The file is replaced in one step, so a crash never leaves half a file, and only you can read it."""
    values = {name: str(value).strip() for name, value in values.items() if value}
    for name, value in values.items():
        if not NAME.fullmatch(name) or re.search(r"\s", value):
            raise ValueError(f"Cannot save {name}: key names are letters, digits and _, and keys have no spaces")
    path = keys_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not os.environ.get("ORION_KEYS_FILE"):
        os.chmod(path.parent, 0o700)  # Our own folder; a custom keys file keeps its folder as it is.
    lines = path.read_text().splitlines() if path.is_file() else []
    for i, line in enumerate(lines):
        name, _ = parse_line(line)
        if name in values:
            lines[i] = f"{name}={values[name]}"
    written = {parse_line(line)[0] for line in lines}
    lines += [f"{name}={value}" for name, value in values.items() if name not in written]
    write_private(path, "\n".join(lines) + "\n")
    return path


def write_private(path, text):
    fd, tmp = tempfile.mkstemp(prefix=".keys-", dir=path.parent)  # Created with mode 0600.
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
