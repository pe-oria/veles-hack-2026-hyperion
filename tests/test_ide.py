import httpx
import pytest

import helpers
from hyperion import ide
from hyperion.ide import IdeError

GOOD = "http://172.17.0.1:3001/api"


@pytest.fixture
def network(monkeypatch) -> dict:
    """A fake network: only the URLs in state["alive"] answer."""
    state = {"alive": {GOOD}, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        base = str(request.url).split("/agent/")[0]
        state["requests"].append(base)
        if base not in state["alive"]:
            raise httpx.ConnectError("no route to host", request=request)
        if request.url.params["path"] == "nginx.yaml":
            return httpx.Response(200, json={"path": "demo/nginx.yaml", "content": "a: 1\n"})
        return httpx.Response(404, json={"error": "File not found"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(ide.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(helpers, "IDE_BACKEND_URL", "http://host.docker.internal:3001/api")
    monkeypatch.setattr(ide, "default_gateway", lambda: "172.17.0.1")
    return state


def test_candidates_start_with_the_configured_url_and_cover_linux(network):
    urls = ide.candidate_urls()
    assert urls[0] == "http://host.docker.internal:3001/api"
    assert GOOD in urls and "http://localhost:3001/api" in urls
    assert len(urls) == len(set(urls))


async def test_discover_switches_to_the_address_that_answers(network):
    assert await ide.discover() == GOOD
    assert helpers.IDE_BACKEND_URL == GOOD


async def test_discover_keeps_the_configured_url_when_it_works(network):
    network["alive"] = {"http://host.docker.internal:3001/api", GOOD}
    assert await ide.discover() == "http://host.docker.internal:3001/api"


async def test_read_recovers_when_host_docker_internal_does_not_resolve(network):
    # plain `docker run` on Linux: the configured name is dead, the gateway works
    file = await ide.read("nginx.yaml")
    assert (file.path, file.content) == ("demo/nginx.yaml", "a: 1\n")
    assert helpers.IDE_BACKEND_URL == GOOD

    network["requests"].clear()
    await ide.read("nginx.yaml")
    assert network["requests"] == [GOOD]  # found once, used directly afterwards


async def test_unreachable_backend_explains_how_to_fix_it(network):
    network["alive"] = set()
    with pytest.raises(IdeError) as error:
        await ide.read("nginx.yaml")
    assert error.value.kind == "unreachable"
    assert "--add-host host.docker.internal:host-gateway" in str(error.value)


async def test_a_web_server_that_is_not_the_backend_is_not_chosen(network, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="<html>nginx</html>", headers={"content-type": "text/html"})

    monkeypatch.undo()  # drop the fixture's network; every address is now a plain web server
    real_client = httpx.AsyncClient
    monkeypatch.setattr(ide.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    assert await ide.discover() is None


def test_default_gateway_parses_the_route_table(tmp_path, monkeypatch):
    table = "Iface\tDestination\tGateway\tFlags\neth0\t00000000\t010011AC\t0003\neth0\t000011AC\t00000000\t0001\n"
    real_open = open
    monkeypatch.setattr("builtins.open", lambda path, *a, **k: real_open(tmp_path / "route", *a, **k)
                        if path == "/proc/net/route" else real_open(path, *a, **k))
    (tmp_path / "route").write_text(table)
    assert ide.default_gateway() == "172.17.0.1"
