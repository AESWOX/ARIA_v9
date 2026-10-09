"""F2: no real-looking provider keys in tracked source/docs."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATTERNS = {
    "google api key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "groq key": re.compile(r"gsk_[0-9A-Za-z]{40,}"),
    "openai/deepseek-style key": re.compile(r"\bsk-[0-9A-Za-z]{32,}"),
    "anthropic key": re.compile(r"sk-ant-[0-9A-Za-z_\-]{20,}"),
    "aws access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}
PLACEHOLDER = re.compile(r"(?i)(x{6,}|\.\.\.|…)")
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".ts", ".tsx", ".js", ".toml", ".yml", ".yaml", ".env", ".bat", ".ps1", ".rs", ".spec"}


def _tracked_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except Exception:
        return None
    return [ROOT / line for line in out.splitlines() if line]


def test_no_secrets_in_tracked_files():
    files = _tracked_files()
    if files is None:  # not a git checkout (e.g. a release tarball): nothing to scan reliably
        return
    hits = []
    for f in files:
        if f.suffix.lower() not in TEXT_SUFFIXES or not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, rx in PATTERNS.items():
            for match in rx.finditer(text):
                if PLACEHOLDER.search(match.group(0)):
                    continue  # fixtures like sk-ant-xxxxxxxx used by the secret-detector tests
                hits.append((str(f.relative_to(ROOT)), name))  # never print the match itself
                break
    assert not hits, f"secret-looking strings found: {hits}"


def test_scanner_actually_detects_keys():
    """Negative control: the patterns must fire on key-shaped strings (built at runtime, not stored in the repo)."""
    assert PATTERNS["google api key"].search("AIza" + "a1B2" * 9)
    assert PATTERNS["groq key"].search("gsk_" + "a1B2" * 12)
    assert PATTERNS["openai/deepseek-style key"].search("sk-" + "a1B2" * 9)
