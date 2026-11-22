from pathlib import Path


ROUTES = Path(__file__).parents[1] / "fontend" / "routes.caddy"


def test_text_search_uses_authenticated_proxy_route():
    routes = ROUTES.read_text(encoding="utf-8")

    assert "@user_api path /api/rag/* /api/chunks/search /api/chunks/search/text" in routes
    assert "@public_api path /api/papers /api/papers/*" in routes
    assert "@public_api path /api/papers /api/papers/* /api/chunks/search/text" not in routes
