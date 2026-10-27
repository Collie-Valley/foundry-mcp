"""FNV-310: el MCP sobre el SDK 2.x, que habla la revisión 2026-07-28 y las anteriores.

Lo que no se ve en el resto de pruebas: que las preguntas (login, confirmar un borrado) van por
resolvers que el SDK manda por el canal de cada protocolo, y que nada se pierde por el camino. El
recorrido real por stdio en los dos protocolos va en la verificación de la tarjeta.
"""

from __future__ import annotations

import inspect
import os
from types import SimpleNamespace
from typing import Any
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from mcp.server.elicitation import render_elicitation_schema
from mcp.server.mcpserver import Elicit
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from foundry_mcp import server
from foundry_mcp.foundry import FoundryError
from foundry_mcp.server import ConfirmForm, LoginForm

PREGUNTA: Any = SimpleNamespace(client_capabilities=SimpleNamespace(elicitation=SimpleNamespace()))
NO_PREGUNTA: Any = SimpleNamespace(client_capabilities=None)
SIN_CREDENCIALES = {"FOUNDRY_TOKEN": "", "FOUNDRY_EMAIL": "", "FOUNDRY_PASSWORD": ""}


class ResolvedParametersTests(IsolatedAsyncioTestCase):
    async def test_every_hidden_parameter_is_really_resolved(self) -> None:
        """Si el SDK no pudiera leer una anotación `Resolve`, la trataría EN SILENCIO como un argumento
        más: el agente podría rellenar `confirmacion` y saltarse la pregunta. Aquí se ve."""
        listed = {t.name: set(t.input_schema.get("properties", {})) for t in await server.mcp.list_tools()}
        for tool in server.mcp._tool_manager.list_tools():
            params = set(inspect.signature(tool.fn).parameters) - {"ctx"}
            with self.subTest(tool=tool.name):
                self.assertEqual(params - listed[tool.name], set(tool.resolved_params))
                self.assertFalse({"borrado", "confirmacion", "credenciales"} & listed[tool.name])

    async def test_the_tools_that_ask_have_their_resolvers(self) -> None:
        for name in ("delete_task", "delete_column", "delete_task_attachment", "delete_note"):
            with self.subTest(tool=name):
                tool = server.mcp._tool_manager.get_tool(name)
                assert tool is not None
                self.assertEqual(set(tool.resolved_params), {"borrado", "confirmacion"})
        login = server.mcp._tool_manager.get_tool("login")
        assert login is not None
        self.assertEqual(set(login.resolved_params), {"credenciales"})


class ElicitationSchemaTests(TestCase):
    def test_the_forms_send_only_what_the_spec_allows_at_the_root(self) -> None:
        """La especificación solo admite `$schema`, `type`, `properties` y `required` en la raíz del
        formulario. Codex se ciñe a eso: con un `title` de más no enseña el diálogo y lo cancela solo,
        así que ni se podía entrar ni confirmar un borrado (salió manejando Codex en FNV-314)."""
        for form in (LoginForm, ConfirmForm):
            with self.subTest(form=form.__name__):
                schema = render_elicitation_schema(form)
                self.assertLessEqual(set(schema), {"$schema", "type", "properties", "required"})
                self.assertEqual(schema["type"], "object")
                self.assertTrue(schema["properties"])


class LoginResolverTests(TestCase):
    def test_a_client_that_can_ask_gets_the_dialog(self) -> None:
        with patch.dict(os.environ, SIN_CREDENCIALES):
            pregunta = server._pedir_credenciales(PREGUNTA)
        assert isinstance(pregunta, Elicit)
        self.assertIs(pregunta.schema, LoginForm)
        self.assertIn("Inicia sesión en Foundry", pregunta.message)

    def test_environment_credentials_enter_without_asking(self) -> None:
        with patch.dict(os.environ, {**SIN_CREDENCIALES, "FOUNDRY_TOKEN": "tok"}):
            self.assertEqual(server._pedir_credenciales(NO_PREGUNTA), LoginForm(token="tok"))
        with patch.dict(os.environ, {**SIN_CREDENCIALES, "FOUNDRY_EMAIL": "a@b.c", "FOUNDRY_PASSWORD": "x"}):
            self.assertEqual(server._pedir_credenciales(PREGUNTA), LoginForm(email="a@b.c", password="x"))

    def test_a_client_that_cannot_ask_is_told_what_to_do(self) -> None:
        with patch.dict(os.environ, SIN_CREDENCIALES), self.assertRaises(FoundryError) as err:
            server._pedir_credenciales(NO_PREGUNTA)
        self.assertIn("FOUNDRY_TOKEN", str(err.exception))
        # La salida sin contraseña en la configuración: entrar desde otro cliente y recordar (OpenCode).
        self.assertIn("«recordar»", str(err.exception))


class ReauthOn2026Tests(IsolatedAsyncioTestCase):
    async def test_without_back_channel_it_points_to_login(self) -> None:
        """En 2026-07-28 no se puede abrir el diálogo a mitad de una herramienta: se dice cómo seguir
        en vez de fallar con un error genérico (o, peor, quedarse esperando)."""
        ctx: Any = SimpleNamespace(protocol_version="2026-07-28", elicit=AsyncMock())
        with (
            patch.dict(os.environ, SIN_CREDENCIALES),
            patch("foundry_mcp.server._keyring_get", return_value=None),
            self.assertRaises(FoundryError) as err,
        ):
            await server._reauth(ctx)
        self.assertIn("`login`", str(err.exception))
        ctx.elicit.assert_not_awaited()

    async def test_older_protocols_still_open_the_dialog_mid_call(self) -> None:
        respuesta = SimpleNamespace(action="accept", data=LoginForm(token="nuevo"))
        ctx: Any = SimpleNamespace(protocol_version="2025-11-25", elicit=AsyncMock(return_value=respuesta))
        with (
            patch.dict(os.environ, SIN_CREDENCIALES),
            patch("foundry_mcp.server._keyring_get", return_value=None),
        ):
            self.assertEqual(await server._reauth(ctx), "nuevo")
        ctx.elicit.assert_awaited_once()


class ReadableErrorsTests(IsolatedAsyncioTestCase):
    async def test_foundry_errors_reach_the_model_with_their_reason(self) -> None:
        """Con el SDK 2.x, una excepción que no sea `ToolError` llega como «Error executing tool», sin
        el motivo. `FoundryError` lo es, así que el agente sigue leyendo qué pasó."""
        cliente = AsyncMock()
        cliente.get_initiative.side_effect = FoundryError("400: la columna «Hecho» ya existe.")
        # `call_tool` lo relanza (la capa de protocolo lo convierte luego en `isError`); lo que importa
        # es que sea un ToolError «previsto», con su texto, y no el genérico de un fallo inesperado.
        with (
            patch("foundry_mcp.server._prepare", return_value=cliente),
            self.assertRaises(ToolError) as err,
        ):
            await server.mcp.call_tool("get_board", {"initiative_id": "ini"})
        self.assertNotIsInstance(err.exception, UnexpectedToolError)
        self.assertIn("la columna «Hecho» ya existe", str(err.exception))
