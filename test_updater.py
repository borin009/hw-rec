import updater


def test_version_tuple_accepts_release_tags():
    assert updater.version_tuple("hw_v1.0.12") == (1, 0, 12)


def test_update_is_returned_only_when_newer(monkeypatch):
    release = {
        "tag_name": "hw_v1.0.2",
        "name": "HW rec v1.0.2",
        "body": "Test update",
        "html_url": "https://example.test/release",
        "assets": [{
            "name": "HW.rec.v1.0.2.exe",
            "browser_download_url": "https://example.test/app.exe",
            "digest": "sha256:" + "a" * 64,
        }],
    }
    monkeypatch.setattr(updater, "_request_json", lambda _url: release)
    assert updater.check_for_update("1.0.1").version == "1.0.2"
    assert updater.check_for_update("1.0.2") is None


def test_release_without_verified_digest_is_rejected(monkeypatch):
    release = {
        "tag_name": "hw_v2.0.0",
        "assets": [{"name": "app.exe", "browser_download_url": "https://example.test"}],
    }
    monkeypatch.setattr(updater, "_request_json", lambda _url: release)
    try:
        updater.check_for_update("1.0.1")
    except RuntimeError as error:
        assert "SHA-256" in str(error)
    else:
        raise AssertionError("Missing digest was accepted")
