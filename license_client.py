"""HW rec licensing client for the Render API."""
import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://hw-rec-api.onrender.com"
CACHE_FILE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "HW rec" / "license.json"


def _powershell_value(expression: str) -> str:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", expression],
        capture_output=True, text=True, timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout.strip() or "unknown"


def pc_identity() -> tuple[str, str]:
    serial = _powershell_value("(Get-CimInstance Win32_BIOS).SerialNumber")
    cpu = _powershell_value("(Get-CimInstance Win32_Processor | Select-Object -First 1).ProcessorId")
    return serial, cpu


def _post(path: str, body: dict, token: str = "") -> dict:
    headers = {"Content-Type": "application/json", "User-Agent": "HW-rec/1.0.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        API_URL + path, json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            detail = json.load(error).get("detail", str(error))
        except Exception:
            detail = str(error)
        raise RuntimeError(detail) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(f"License server unavailable: {error}") from error


def login(license_key: str) -> dict:
    serial, cpu = pc_identity()
    result = _post("/v1/login", {"license_key": license_key, "serial": serial, "cpu": cpu})
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps({**result, "serial": serial, "cpu": cpu}), encoding="utf-8")
    return result


def cached_session() -> dict | None:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        payload = data["access_token"].split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        if int(claims["exp"]) > int(time.time()) + 60:
            return data
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        pass
    return None
