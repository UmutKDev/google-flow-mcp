import base64
import json

import pytest

from google_flow_mcp.cookies import CookieError, parse_cookies, summarise

SESSION = {"name": "__Secure-1PSID", "value": "v", "domain": ".google.com", "path": "/",
           "expires": 4102444800, "secure": True, "httpOnly": True, "sameSite": "no_restriction"}


def test_storage_state_and_filtering():
    state = {"cookies": [SESSION, {"name": "x", "value": "y", "domain": ".evil.com"}], "origins": []}
    cookies = parse_cookies(json.dumps(state))
    assert [c["name"] for c in cookies] == ["__Secure-1PSID"]
    assert cookies[0]["sameSite"] == "None"


def test_cookie_editor_array_and_base64():
    raw = json.dumps([{**SESSION, "expirationDate": 4102444800.5, "expires": None}])
    assert parse_cookies(raw)[0]["expires"] == 4102444800.5
    assert parse_cookies(base64.b64encode(raw.encode()).decode())[0]["name"] == "__Secure-1PSID"


def test_netscape():
    txt = "# Netscape HTTP Cookie File\n#HttpOnly_.google.com\tTRUE\t/\tTRUE\t4102444800\tSID\tabc\n"
    cookies = parse_cookies(txt)
    assert cookies[0]["name"] == "SID" and cookies[0]["httpOnly"]


def test_rejects_without_session_cookie():
    with pytest.raises(CookieError):
        parse_cookies(json.dumps([{"name": "NID", "value": "1", "domain": ".google.com"}]))
    with pytest.raises(CookieError):
        parse_cookies("not cookies")


def test_summary_never_has_values():
    s = summarise(parse_cookies(json.dumps([SESSION])))
    assert s["count"] == 1 and s["session_cookie_expires_in_days"] > 0
    assert "value" not in json.dumps(s)
