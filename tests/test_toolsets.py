"""FNV-313: lo que pesan las herramientas en cada sesión, sus títulos y los grupos activables."""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr
from typing import Any
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

from mcp.server.fastmcp import FastMCP

from foundry_mcp import server
from foundry_mcp.config import load_config
from foundry_mcp.server import ALWAYS_ON, TITLES, TOOLSETS, _finish_tools, _selected_tools

# Descripciones + esquemas de entrada de la 0.1.1 medían 50 202 caracteres; el criterio era bajar al
# menos un 30 %. El tope deja algo de holgura para herramientas nuevas, pero avisa si se engorda.
MAX_DESCRIPTIONS_AND_SCHEMAS = 36_000


class RegistryTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tools = await server.mcp.list_tools()
        self.names = {t.name for t in self.tools}

    async def test_every_tool_has_a_readable_title(self) -> None:
        for t in self.tools:
            with self.subTest(tool=t.name):
                self.assertTrue(t.title)
                self.assertNotEqual(t.title, t.name)

    async def test_every_tool_is_in_exactly_one_group(self) -> None:
        """Una herramienta nueva sin grupo quedaría fuera en cuanto alguien use FOUNDRY_TOOLSETS."""
        grouped = [n for names in TOOLSETS.values() for n in names] + list(ALWAYS_ON)
        self.assertEqual(sorted(grouped), sorted(set(grouped)), "una herramienta en dos grupos")
        self.assertEqual(set(grouped), self.names)
        self.assertEqual(set(TITLES), self.names)

    async def test_no_generic_output_schema_and_no_argument_titles(self) -> None:
        for t in self.tools:
            with self.subTest(tool=t.name):
                self.assertIsNone(t.outputSchema)
                self.assertNotIn("title", t.inputSchema)
                for prop in t.inputSchema.get("properties", {}).values():
                    self.assertNotIn("title", prop)

    async def test_size_stays_under_budget(self) -> None:
        size = sum(
            len(t.description or "") + len(json.dumps(t.inputSchema, ensure_ascii=False)) for t in self.tools
        )
        self.assertLess(size, MAX_DESCRIPTIONS_AND_SCHEMAS)

    async def test_hard_rules_survive_the_diet(self) -> None:
        """Adelgazar no puede costar las reglas que evitan errores caros."""
        desc = {t.name: (t.description or "").lower() for t in self.tools}
        self.assertIn("categoría", desc["get_board"])
        self.assertIn("exige padre", desc["create_task"])
        self.assertIn("exige padre", desc["set_task_hierarchy"])
        self.assertIn("exactamente una", desc["upload_task_attachment"])
        for gate in ("review_criterion", "review_decision", "review_task_proposal"):
            self.assertIn("nunca", desc[gate])
        for destructive in ("delete_task", "delete_note"):
            self.assertIn("pregunta", desc[destructive])
        self.assertIn("append", desc["write_note"])
        instructions = (server.mcp.instructions or "").lower()
        self.assertIn("colaborador", instructions)
        self.assertIn("gates humanos", instructions)

    async def test_calls_return_plain_content_once(self) -> None:
        """Sin outputSchema, el resultado viaja una vez (texto), no también como structuredContent."""
        result = await server.mcp.call_tool("logout", {})
        self.assertIsInstance(result, list)


class SelectedToolsTests(TestCase):
    def test_without_groups_everything(self) -> None:
        everything = _selected_tools(())
        self.assertEqual(everything, _selected_tools(("todos",)))
        self.assertIn("list_notes", everything)
        self.assertIn("create_sprint", everything)

    def test_one_group_plus_session(self) -> None:
        notes = _selected_tools(("notas",))
        self.assertEqual(notes, set(ALWAYS_ON) | set(TOOLSETS["notas"]))
        self.assertNotIn("create_sprint", notes)

    def test_unknown_group_is_warned_and_ignored(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            selected = _selected_tools(("sprints", "inventado"))
        self.assertEqual(selected, set(ALWAYS_ON) | set(TOOLSETS["sprints"]))
        self.assertIn("inventado", err.getvalue())

    def test_env_variable_is_parsed(self) -> None:
        with patch.dict(os.environ, {"FOUNDRY_TOOLSETS": " Tableros, notas ,"}, clear=False):
            self.assertEqual(load_config().toolsets, ("tableros", "notas"))
        with patch.dict(os.environ, {"FOUNDRY_TOOLSETS": ""}, clear=False):
            self.assertEqual(load_config().toolsets, ())


class FinishToolsTests(IsolatedAsyncioTestCase):
    async def test_it_prunes_titles_and_strips_on_a_fresh_server(self) -> None:
        fresh = FastMCP("prueba")

        async def whoami() -> dict[str, Any]:
            return {}

        async def list_notes(space_id: str) -> list[dict[str, Any]]:
            return []

        async def create_sprint(initiative_id: str, name: str) -> dict[str, Any]:
            return {}

        for fn in (whoami, list_notes, create_sprint):
            fresh.tool()(fn)
        _finish_tools(fresh, ("notas",))
        tools = {t.name: t for t in await fresh.list_tools()}
        self.assertEqual(set(tools), {"whoami", "list_notes"})
        self.assertEqual(tools["list_notes"].title, "Listar notas")
        self.assertIsNone(tools["list_notes"].outputSchema)
        self.assertNotIn("title", tools["list_notes"].inputSchema["properties"]["space_id"])
