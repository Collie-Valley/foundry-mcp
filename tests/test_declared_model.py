"""FNV-311: el modelo se declara una vez y llega a todo lo que el agente hace en la sesión.

Se prueba de punta a punta (herramienta → cabeceras de la petición), porque el fallo que arregla
estaba justo ahí: las herramientas sin el parámetro `agent_model` mandaban la petición sin modelo.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx

from foundry_mcp import server
from foundry_mcp.foundry import FoundryClient, set_agent_identity


def _ctx(client_name: str) -> Any:
    """Lo mínimo de un `Context` que mira el servidor: el `clientInfo` de la sesión."""
    info = SimpleNamespace(name=client_name, title=None)
    return SimpleNamespace(session=SimpleNamespace(client_params=SimpleNamespace(clientInfo=info)))


class DeclaredModelEndToEndTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        server._declared_model = ""
        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.path.endswith("/me"):
                return httpx.Response(200, json={"id": "u1", "email": "a@b.c", "display_name": "A"})
            if request.method == "GET":  # el tablero, para resolver la columna por nombre o id
                column = {"id": "column-1", "name": "Por hacer", "category": "todo", "order": 0}
                return httpx.Response(200, json={"id": "initiative-1", "board": {"columns": [column]}})
            return httpx.Response(200, json={"id": "x1"})

        self.client = FoundryClient("https://foundry.example/api/v1")
        await self.client._c.aclose()
        self.client._c = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.client.token = "test-token"
        self._patch = patch("foundry_mcp.server._get_client", return_value=self.client)
        self._patch.start()
        # Sin modelo por entorno: lo que se ve tiene que salir de la declaración.
        self._env = patch.dict("os.environ", {"FOUNDRY_AGENT_MODEL": ""}, clear=False)
        self._env.start()

    async def asyncTearDown(self) -> None:
        self._patch.stop()
        self._env.stop()
        server._declared_model = ""
        set_agent_identity(None, None)
        await self.client._c.aclose()

    async def test_whoami_declares_and_reports_the_identity(self) -> None:
        me = await server.whoami(_ctx("codex-mcp-client"), agent_model="gpt-6-luna")
        self.assertEqual(me["email"], "a@b.c")
        self.assertEqual(me["agente"], {"agent_source": "codex", "agent_model": "gpt-6-luna"})

    async def test_tools_without_the_parameter_carry_the_declared_model(self) -> None:
        """Renombrar una columna (no tiene `agent_model`) tras declarar en `whoami` lleva el modelo."""
        ctx = _ctx("opencode")
        await server.whoami(ctx, agent_model="qwen3-coder")
        self.requests.clear()
        await server.rename_column(ctx, "initiative-1", "column-1", "Probando")
        self.assertTrue(self.requests, "la herramienta no llegó a llamar a la API")
        for request in self.requests:
            self.assertEqual(request.headers["x-foundry-agent-source"], "opencode")
            self.assertEqual(request.headers["x-foundry-agent-model"], "qwen3-coder")

    async def test_without_declaration_no_model_header(self) -> None:
        await server.whoami(_ctx("claude-code"))
        self.assertNotIn("x-foundry-agent-model", self.requests[-1].headers)
        self.assertEqual(self.requests[-1].headers["x-foundry-agent-source"], "claude_code")
