"""Secure GitHub Releases updater for HW rec."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path


LATEST_RELEASE_URL = "https://api.github.com/repos/borin009/hw-rec/releases/latest"
USER_AGENT = "HW-rec-updater"


@dataclass(frozen=True)
class UpdateRelease:
    version: str
    title: str
    notes: str
    page_url: str
    download_url: str
    sha256: str


def version_tuple(value: str) -> tuple[int, ...]:
    match = re.search(r"(\d+(?:\.\d+)+)", value)
    if not match:
        raise ValueError(f"Invalid version: {value}")
    return tuple(int(part) for part in match.group(1).split("."))


def _request_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def check_for_update(current_version: str) -> UpdateRelease | None:
    release = _request_json(LATEST_RELEASE_URL)
    version = re.search(r"\d+(?:\.\d+)+", str(release.get("tag_name", "")))
    if not version or version_tuple(version.group()) <= version_tuple(current_version):
        return None

    assets = release.get("assets") or []
    executable = next(
        (asset for asset in assets if str(asset.get("name", "")).lower().endswith(".exe")),
        None,
    )
    if not executable:
        raise RuntimeError("The latest release does not contain a Windows executable.")

    digest = str(executable.get("digest") or "")
    if not digest.lower().startswith("sha256:"):
        raise RuntimeError("The release executable has no GitHub SHA-256 digest.")

    return UpdateRelease(
        version=version.group(),
        title=str(release.get("name") or release.get("tag_name") or "HW rec update"),
        notes=str(release.get("body") or ""),
        page_url=str(release.get("html_url") or ""),
        download_url=str(executable["browser_download_url"]),
        sha256=digest.split(":", 1)[1].lower(),
    )


def download_verified(release: UpdateRelease) -> Path:
    update_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "HW rec" / "updates"
    update_dir.mkdir(parents=True, exist_ok=True)
    partial = update_dir / f"HW-rec-v{release.version}.exe.part"
    completed = partial.with_suffix("")
    request = urllib.request.Request(release.download_url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
        if digest.hexdigest().lower() != release.sha256:
            raise RuntimeError("Downloaded update failed SHA-256 verification.")
        os.replace(partial, completed)
        return completed
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def launch_replacement(downloaded_exe: Path) -> None:
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Automatic installation is available only in the packaged app.")
    current_exe = Path(sys.executable).resolve()
    script = downloaded_exe.parent / "install-update.cmd"
    # The generated script contains only paths selected by this application.
    script.write_text(
        "@echo off\n"
        "timeout /t 2 /nobreak >nul\n"
        f'copy /y "{downloaded_exe}" "{current_exe}" >nul\n'
        "if errorlevel 1 (\n"
        f'  start "" "{downloaded_exe}"\n'
        ") else (\n"
        f'  start "" "{current_exe}"\n'
        f'  del /q "{downloaded_exe}"\n'
        ")\n"
        'del /q "%~f0"\n',
        encoding="utf-8",
    )
    subprocess.Popen(
        ["cmd.exe", "/d", "/c", str(script)],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        close_fds=True,
    )
