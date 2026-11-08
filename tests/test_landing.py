from google_flow_mcp.browser import landing_kind


def test_landing_kind():
    assert landing_kind("https://flow.google.com/about") == "public"
    assert landing_kind("https://flow.google.com/about/") == "public"
    assert landing_kind("https://accounts.google.com/v3/signin/confirmidentifier?x=1") == "signin"
    assert landing_kind("https://flow.google.com/") is None
    assert landing_kind("https://flow.google.com/project/0f0e4a5c-1111-2222-3333-444455556666") is None
    assert landing_kind("not a url") is None
