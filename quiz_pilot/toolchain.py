"""Portable C++ toolchain for laptops with no compiler installed.

Many users have no WSL, no MinGW and no MSYS2. Rather than asking
each of them to install a toolchain by hand, the native backend can
fetch a standalone compiler into the project folder:

- WinLibs standalone MinGW-w64 build (GCC 15.2.0, POSIX threads,
  UCRT, x86_64), pinned by URL + SHA-256 + size, verified against
  the GitHub release API on 2026-11-13. The .zip extracts with
  Python's stdlib zipfile - no 7-Zip, no admin, no PATH changes,
  no system changes at all. Everything lives under `.tools/` in
  the project folder and is deletable like any other folder.
- The archive is streamed to disk while its SHA-256 is computed;
  a size or hash mismatch deletes the partial file and fails
  honestly instead of extracting something untrusted.
- Extraction rejects absolute paths and `..` members (zip-slip).
- If a compiler already exists on PATH or in `.tools/`, nothing
  is downloaded: the existing one wins.

The pinned build also ships GNU Make, CMake, GDB and ninja, so
starter-code Makefiles work too. UCRT requires Windows 10+ (or
the Universal C Runtime update on older Windows).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional

TOOLCHAIN_URL = ("https://github.com/brechtsanders/winlibs_mingw/releases/download/"
                 "15.2.0posix-13.0.0-ucrt-r4/"
                 "winlibs-x86_64-posix-seh-gcc-15.2.0-mingw-w64ucrt-13.0.0-r4.zip")
TOOLCHAIN_SHA256 = "cd140e153bf303703f9dce27a4b7d0a8dcb2a41c39b7fffa477a12a8bd049bca"
TOOLCHAIN_SIZE = 265050288  # bytes, per the GitHub release API
TOOLCHAIN_SUBDIR = "mingw64"
ARCHIVE_NAME = "winlibs-mingw.zip"

_BIN_CANDIDATES = ("g++.exe", "g++")


def default_tools_dir(base: Optional[str] = None) -> Path:
    """Project-local tools folder: <cwd>/.tools (override `base` in tests)."""
    return Path(base or os.getcwd()) / ".tools"


def _local_bin_dir(tools_dir: Optional[Path] = None) -> Path:
    return (tools_dir or default_tools_dir()) / TOOLCHAIN_SUBDIR / "bin"


def find_compiler(
    _which: Callable[[str], Optional[str]] = shutil.which,
    tools_dir: Optional[Path] = None,
) -> Optional[str]:
    """Path to a usable g++, preferring PATH, else the local toolchain."""
    on_path = _which("g++")
    if on_path:
        return on_path
    bin_dir = _local_bin_dir(tools_dir)
    for name in _BIN_CANDIDATES:
        candidate = bin_dir / name
        if candidate.is_file():
            return str(candidate)
    return None


def compiler_status(
    _which: Callable[[str], Optional[str]] = shutil.which,
    tools_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """{"found", "path", "source"} with source path|local|none."""
    on_path = _which("g++")
    if on_path:
        return {"found": True, "path": on_path, "source": "path"}
    bin_dir = _local_bin_dir(tools_dir)
    for name in _BIN_CANDIDATES:
        candidate = bin_dir / name
        if candidate.is_file():
            return {"found": True, "path": str(candidate), "source": "local"}
    return {"found": False, "path": None, "source": "none"}


def _download_verified(url: str, dest: Path, expected_sha256: str, expected_size: int) -> None:
    """Stream `url` to `dest`, verifying size + SHA-256 as it lands."""
    hasher = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(url, timeout=600) as resp, open(dest, "wb") as fh:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)
            hasher.update(chunk)
            total += len(chunk)
    if total != expected_size:
        raise ValueError(f"download size mismatch: got {total} bytes, expected {expected_size}")
    if hasher.hexdigest() != expected_sha256:
        raise ValueError("download SHA-256 mismatch - refusing to use the archive")


def _extract_zip(archive: Path, dest: Path) -> None:
    """Extract a zip, rejecting absolute paths and `..` members."""
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            target = (dest / member.filename).resolve()
            if not str(target).startswith(str(dest.resolve()) + os.sep) and target != dest.resolve():
                raise ValueError(f"unsafe zip member: {member.filename!r}")
            zf.extract(member, dest)


def bootstrap_toolchain(
    tools_dir: Optional[Path] = None,
    _download: Optional[Callable[..., None]] = None,
    _extract: Optional[Callable[[Path, Path], None]] = None,
    _which: Callable[[str], Optional[str]] = shutil.which,
) -> Dict[str, Any]:
    """Ensure a local portable toolchain exists; download it if needed.

    If any compiler already exists (PATH or local), nothing is
    downloaded. Returns {"ok": True, "bin_dir", "compiler",
    "already_present"} on success, {"ok": False, "error"} on any
    failure. Never raises for expected failures (network, bad hash,
    missing compiler after extract): those come back as error dicts.
    """
    tools_dir = tools_dir or default_tools_dir()
    bin_dir = _local_bin_dir(tools_dir)
    status = compiler_status(_which=_which, tools_dir=tools_dir)
    if status["found"]:
        return {"ok": True,
                "bin_dir": (str(bin_dir) if status["source"] == "local"
                            else str(Path(status["path"]).parent)),
                "compiler": status["path"], "already_present": True}
    tools_dir.mkdir(parents=True, exist_ok=True)
    archive = tools_dir / ARCHIVE_NAME
    download = _download or _download_verified
    extract = _extract or _extract_zip
    try:
        download(TOOLCHAIN_URL, archive, TOOLCHAIN_SHA256, TOOLCHAIN_SIZE)
        extract(archive, tools_dir)
    except Exception as e:  # network, hash/size mismatch, bad zip, ...
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": False, "error": f"toolchain download failed: {e}"}
    finally:
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass
    for name in _BIN_CANDIDATES:
        candidate = bin_dir / name
        if candidate.is_file():
            return {"ok": True, "bin_dir": str(bin_dir), "compiler": str(candidate),
                    "already_present": False}
    return {"ok": False,
            "error": "toolchain extracted but no g++ found under mingw64/bin"}
