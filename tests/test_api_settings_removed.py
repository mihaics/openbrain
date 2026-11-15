"""B2: GET /settings returned config/settings.yaml (including the database password) and
PUT /settings overwrote it. Both routes must be gone. The check reads the route table only:
no request is sent and the app lifespan (database init) is not entered."""


def test_settings_routes_removed():
    from src.api.main import app

    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/settings" not in paths
