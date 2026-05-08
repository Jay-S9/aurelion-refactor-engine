"""
Aurelion Refactor Engine v2 - File Safety Guard
Prevents accidental modification of binary files.
Provides safe encoding detection with graceful fallback.

NEW IN v2:
  - Binary file detection via null-byte sniffing + known binary extensions
  - Encoding probe: tries utf-8, then latin-1 as safe fallback
  - SAFE_TEXT_EXTENSIONS whitelist (overridable via --include-binary)
  - File size guard for large-file streaming path
"""

from pathlib import Path
from typing import Tuple

# ── Whitelist of extensions treated as safe text by default ──────────────────
SAFE_TEXT_EXTENSIONS: frozenset[str] = frozenset({
    # Source code
    ".py", ".pyw", ".pyi",
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".java", ".kt", ".scala", ".groovy",
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp",
    ".cs", ".vb",
    ".go", ".rs", ".swift",
    ".rb", ".php", ".pl", ".pm",
    ".lua", ".r", ".m",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    # Data / config
    ".json", ".jsonc", ".json5",
    ".toml", ".yaml", ".yml",
    ".ini", ".cfg", ".conf", ".env", ".properties",
    ".xml", ".xsd", ".xsl", ".xslt", ".svg",
    ".csv", ".tsv",
    ".sql", ".graphql", ".gql",
    # Docs / markup
    ".md", ".mdx", ".rst", ".txt", ".text",
    ".html", ".htm", ".xhtml",
    ".css", ".scss", ".sass", ".less",
    ".tex", ".bib",
    # Build / project
    ".gradle", ".cmake", ".make", ".mk",
    ".dockerfile", ".containerfile",
    ".gitignore", ".gitattributes", ".editorconfig",
    ".lock",            # package-lock, Cargo.lock, etc.
    ".mod",             # go.mod
})

# ── Extensions that are definitively binary ───────────────────────────────────
KNOWN_BINARY_EXTENSIONS: frozenset[str] = frozenset({
    # Compiled
    ".pyc", ".pyd", ".pyo", ".class", ".o", ".obj", ".exe", ".dll", ".so", ".dylib",
    # Archives
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".whl", ".jar", ".war",
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".tiff", ".webp", ".svg",
    # Media
    ".mp3", ".mp4", ".wav", ".ogg", ".flac", ".avi", ".mov", ".mkv",
    # Documents (binary formats)
    ".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".ods",
    # Fonts
    ".ttf", ".otf", ".woff", ".woff2",
    # Data
    ".pkl", ".db", ".sqlite", ".sqlite3",
})

# Threshold: files larger than this use streaming path in text engine
LARGE_FILE_THRESHOLD_BYTES: int = 5 * 1024 * 1024  # 5 MB

# Bytes to read for binary sniffing
_SNIFF_SIZE: int = 8192


def is_safe_text_file(path: Path, include_binary: bool = False) -> Tuple[bool, str]:
    """
    Determine whether a file is safe to process as text.

    Returns:
        (is_safe: bool, reason: str)
        reason explains why a file was blocked (for logging).

    Strategy (in order):
      1. If include_binary=True, always allow.
      2. If extension is in KNOWN_BINARY_EXTENSIONS, block immediately.
      3. If extension is in SAFE_TEXT_EXTENSIONS, allow without sniffing.
      4. For unknown extensions: sniff first 8 KB for null bytes.
    """
    if include_binary:
        return True, "binary override active"

    suffix = path.suffix.lower()

    if suffix in KNOWN_BINARY_EXTENSIONS:
        return False, f"known binary extension ({suffix})"

    if suffix in SAFE_TEXT_EXTENSIONS:
        return True, "safe text extension"

    # Unknown extension: sniff for null bytes
    try:
        with open(path, "rb") as f:
            chunk = f.read(_SNIFF_SIZE)
        if b"\x00" in chunk:
            return False, f"binary content detected (null bytes in first {_SNIFF_SIZE} bytes)"
        return True, "passed binary sniff (no null bytes)"
    except (OSError, PermissionError) as e:
        return False, f"unreadable: {e}"


def detect_encoding(path: Path, preferred: str = "utf-8") -> Tuple[str, bool]:
    """
    Attempt to detect a usable encoding for a file.

    Strategy:
      1. Try preferred encoding (default: utf-8, strict).
      2. Fall back to utf-8-sig (handles BOM).
      3. Fall back to latin-1 (never fails — all byte values valid).

    Returns:
        (encoding: str, was_fallback: bool)
        was_fallback=True means the preferred encoding failed.
    """
    candidates = [preferred, "utf-8-sig", "latin-1"]

    # Deduplicate while preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for enc in candidates:
        if enc not in seen:
            seen.add(enc)
            ordered.append(enc)

    with open(path, "rb") as f:
        raw = f.read(LARGE_FILE_THRESHOLD_BYTES)

    for i, enc in enumerate(ordered):
        try:
            raw.decode(enc, errors="strict")
            return enc, (i > 0)
        except (UnicodeDecodeError, LookupError):
            continue

    # Should never reach here (latin-1 accepts all bytes), but be safe
    return "latin-1", True


def is_large_file(path: Path) -> bool:
    """Return True if the file exceeds LARGE_FILE_THRESHOLD_BYTES."""
    try:
        return path.stat().st_size > LARGE_FILE_THRESHOLD_BYTES
    except OSError:
        return False
