"""Portable toolchain bootstrap (2026-11-13): the build must work on
any laptop, including ones with no compiler and no WSL.

- find_compiler prefers g++ on PATH, else the project-local
  .tools/mingw64/bin toolchain, else None.
- bootstrap_toolchain downloads the pinned WinLibs MinGW-w64 zip,
  verifies size + SHA-256 while streaming, extracts with a zip-slip
  guard, and verifies g++ actually landed. Failures are error dicts.
- No real network here: the download path is exercised against a
  local file:// URL with a crafted zip.
"""
import hashlib
import zipfile
from pathlib import Path

import pytest

from quiz_pilot import toolchain


def _craft_zip(path, members):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _pin_to_local(monkeypatch, zip_path):
    data = zip_path.read_bytes()
    monkeypatch.setattr(toolchain, "TOOLCHAIN_URL", zip_path.as_uri())
    monkeypatch.setattr(toolchain, "TOOLCHAIN_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(toolchain, "TOOLCHAIN_SIZE", len(data))


# ---------- discovery ----------

def test_find_compiler_prefers_path(tmp_path):
    assert toolchain.find_compiler(_which=lambda n: r"C:\gcc\bin\g++.exe",
                                   tools_dir=tmp_path) == r"C:\gcc\bin\g++.exe"


def test_find_compiler_falls_back_to_local(tmp_path):
    bin_dir = tmp_path / "mingw64" / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / "g++.exe"
    exe.write_bytes(b"fake")
    assert toolchain.find_compiler(_which=lambda n: None, tools_dir=tmp_path) == str(exe)


def test_find_compiler_none_when_nothing_exists(tmp_path):
    assert toolchain.find_compiler(_which=lambda n: None, tools_dir=tmp_path) is None
    status = toolchain.compiler_status(_which=lambda n: None, tools_dir=tmp_path)
    assert status == {"found": False, "path": None, "source": "none"}


def test_compiler_status_sources(tmp_path):
    assert toolchain.compiler_status(_which=lambda n: "g++", tools_dir=tmp_path)["source"] == "path"
    bin_dir = tmp_path / "mingw64" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "g++.exe").write_bytes(b"fake")
    status = toolchain.compiler_status(_which=lambda n: None, tools_dir=tmp_path)
    assert status["source"] == "local" and status["found"] is True


# ---------- bootstrap ----------

def test_bootstrap_short_circuits_when_local_exists(tmp_path):
    bin_dir = tmp_path / "mingw64" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "g++.exe").write_bytes(b"fake")

    def no_download(*a, **k):
        raise AssertionError("must not download")

    res = toolchain.bootstrap_toolchain(_which=lambda n: None, tools_dir=tmp_path, _download=no_download)
    assert res["ok"] is True and res["already_present"] is True
    assert res["bin_dir"] == str(bin_dir)


def test_bootstrap_download_extract_verify_end_to_end(tmp_path, monkeypatch):
    zip_path = _craft_zip(tmp_path / "src.zip",
                          {"mingw64/bin/g++.exe": b"fake-gpp",
                           "mingw64/bin/make.exe": b"fake-make"})
    _pin_to_local(monkeypatch, zip_path)
    res = toolchain.bootstrap_toolchain(_which=lambda n: None, tools_dir=tmp_path / "tools")
    assert res["ok"] is True and res["already_present"] is False
    assert Path(res["compiler"]).is_file()
    assert (tmp_path / "tools" / "mingw64" / "bin" / "make.exe").is_file()
    assert not (tmp_path / "tools" / toolchain.ARCHIVE_NAME).exists()


def test_bootstrap_rejects_hash_mismatch(tmp_path, monkeypatch):
    zip_path = _craft_zip(tmp_path / "src.zip", {"mingw64/bin/g++.exe": b"x"})
    _pin_to_local(monkeypatch, zip_path)
    monkeypatch.setattr(toolchain, "TOOLCHAIN_SHA256", "0" * 64)
    res = toolchain.bootstrap_toolchain(_which=lambda n: None, tools_dir=tmp_path / "tools")
    assert res["ok"] is False and "SHA-256" in res["error"]
    assert not (tmp_path / "tools" / toolchain.ARCHIVE_NAME).exists()


def test_bootstrap_rejects_size_mismatch(tmp_path, monkeypatch):
    zip_path = _craft_zip(tmp_path / "src.zip", {"mingw64/bin/g++.exe": b"x"})
    _pin_to_local(monkeypatch, zip_path)
    monkeypatch.setattr(toolchain, "TOOLCHAIN_SIZE", 999999)
    res = toolchain.bootstrap_toolchain(_which=lambda n: None, tools_dir=tmp_path / "tools")
    assert res["ok"] is False and "size mismatch" in res["error"]


def test_bootstrap_rejects_zip_slip(tmp_path, monkeypatch):
    zip_path = _craft_zip(tmp_path / "src.zip",
                          {"mingw64/bin/g++.exe": b"x", "../evil.txt": b"evil"})
    _pin_to_local(monkeypatch, zip_path)
    res = toolchain.bootstrap_toolchain(_which=lambda n: None, tools_dir=tmp_path / "tools")
    assert res["ok"] is False and "unsafe zip member" in res["error"]
    assert not (tmp_path / "evil.txt").exists()


def test_bootstrap_fails_when_no_compiler_after_extract(tmp_path, monkeypatch):
    zip_path = _craft_zip(tmp_path / "src.zip", {"something/else.txt": b"x"})
    _pin_to_local(monkeypatch, zip_path)
    res = toolchain.bootstrap_toolchain(_which=lambda n: None, tools_dir=tmp_path / "tools")
    assert res["ok"] is False and "no g++ found" in res["error"]


def test_bootstrap_reports_download_failure(tmp_path):
    def boom(*a, **k):
        raise ConnectionError("network down")

    res = toolchain.bootstrap_toolchain(_which=lambda n: None, tools_dir=tmp_path / "tools", _download=boom)
    assert res["ok"] is False and "network down" in res["error"]


def test_pinned_toolchain_constants_are_well_formed():
    assert toolchain.TOOLCHAIN_URL.startswith(
        "https://github.com/brechtsanders/winlibs_mingw/releases/download/")
    assert toolchain.TOOLCHAIN_URL.endswith(".zip")
    assert len(toolchain.TOOLCHAIN_SHA256) == 64
    int(toolchain.TOOLCHAIN_SHA256, 16)
    assert toolchain.TOOLCHAIN_SIZE > 100_000_000
