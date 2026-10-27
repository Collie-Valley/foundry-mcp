"""FNV-282: `ctx` lo inyecta FastMCP por su anotación; nunca es un argumento de la herramienta."""

from __future__ import annotations

import inspect
from unittest import IsolatedAsyncioTestCase, TestCase

from foundry_mcp import server


class ContextInjectionTests(IsolatedAsyncioTestCase):
    """FastMCP reconoce el parámetro de contexto mirando su anotación de tipo.

    Tipar `Context` con sus parámetros (lo que pide mypy en modo estricto) cambia justo esa
    anotación. Si FastMCP dejara de reconocerla, `ctx` pasaría a ser un argumento más de cada
    herramienta: el cliente lo pediría y el diálogo de login o de borrado dejaría de funcionar.
    """

    async def test_no_tool_exposes_ctx_as_an_argument(self) -> None:
        for t in await server.mcp.list_tools():
            with self.subTest(tool=t.name):
                self.assertNotIn("ctx", t.input_schema.get("properties", {}))


class ContextIsInjectedTests(TestCase):
    def test_every_tool_that_takes_ctx_gets_it_injected(self) -> None:
        tools = server.mcp._tool_manager.list_tools()
        con_ctx = [t for t in tools if "ctx" in inspect.signature(t.fn).parameters]
        self.assertTrue(con_ctx)  # si nadie recibe ctx, este test no estaría probando nada
        for t in con_ctx:
            with self.subTest(tool=t.name):
                self.assertEqual(t.context_kwarg, "ctx")
