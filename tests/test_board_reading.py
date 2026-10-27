"""FNV-312: leer un tablero grande sin desbordar el contexto del agente.

El FNV real pesa 590 000 caracteres por `get_board`, el 80 % en la columna de cierre. Aquí un
tablero de juguete con la misma forma que devuelve la API.
"""

from __future__ import annotations

import copy
import json
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from foundry_mcp import server
from foundry_mcp.foundry import FoundryError

LONG = "Descripción larguísima. " * 200


def _task(seq: int, title: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": f"t{seq}", "seq": seq, "title": title, "description": LONG, "priority": "medium",
        "issue_type": "tarea", "labels": [], "parent_id": None, "assignee_id": None,
        "flagged": False, "gate_pending": 0, "tests_total": 0, "tests_passed": 0,
        "comment_count": 0, "description_provenance": "humano", "reporter_id": "u1",
    }  # fmt: skip
    base.update(extra)
    return base


BOARD: dict[str, Any] = {
    "id": "b1",
    "title": "Foundry - new version",
    "key": "FNV",
    "board": {
        "template": "kanban",
        "columns": [
            {"id": "c-back", "name": "Backlog", "order": 0, "category": "todo", "tasks": [
                _task(1, "Migrar el login", labels=[{"id": "l1", "name": "Backend", "color": None}]),
                _task(2, "Añadir búsqueda", issue_type="historia", gate_pending=2),
                _task(3, "Otra cosa"),
            ]},
            {"id": "c-prog", "name": "En progreso", "order": 1, "category": "in_progress", "tasks": [
                _task(4, "Arreglar el hook", issue_type="bug", flagged=True,
                      labels=[{"id": "l1", "name": "backend", "color": None},
                              {"id": "l2", "name": "mcp", "color": None}]),
            ]},
            {"id": "c-done", "name": "Hecho", "order": 2, "category": "done", "tasks": [
                _task(5, "Búsqueda vieja ya cerrada"), _task(6, "Algo cerrado"),
            ]},
        ],
    },
}  # fmt: skip


class BoardReadingTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        client = MagicMock()
        client.get_initiative = AsyncMock(side_effect=lambda _id: copy.deepcopy(BOARD))
        self._patch = patch("foundry_mcp.server._prepare", return_value=client)
        self._patch.start()
        self.ctx: Any = object()

    async def asyncTearDown(self) -> None:
        self._patch.stop()

    def _columns(self, data: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {c["name"]: c for c in data["board"]["columns"]}

    # ── get_board ────────────────────────────────────────────────────────────
    async def test_default_folds_done_and_drops_descriptions(self) -> None:
        cols = self._columns(await server.get_board(self.ctx, "b1"))
        self.assertEqual([t["key"] for t in cols["Backlog"]["tasks"]], ["FNV-1", "FNV-2", "FNV-3"])
        self.assertEqual(cols["Hecho"]["tasks"], [])
        self.assertEqual((cols["Hecho"]["task_count"], cols["Hecho"]["tasks_omitted"]), (2, 2))
        for col in cols.values():
            for task in col["tasks"]:
                self.assertNotIn("description", task)
                self.assertNotIn("description_provenance", task)

    async def test_summary_keeps_what_decides_and_skips_empties(self) -> None:
        cols = self._columns(await server.get_board(self.ctx, "b1"))
        hook = cols["En progreso"]["tasks"][0]
        self.assertEqual(hook["labels"], ["backend", "mcp"])  # nombres, no objetos
        self.assertTrue(hook["flagged"])
        self.assertEqual(cols["Backlog"]["tasks"][1]["gate_pending"], 2)
        plain = cols["Backlog"]["tasks"][2]
        for empty in ("labels", "flagged", "gate_pending", "parent_id", "tests_total"):
            self.assertNotIn(empty, plain)

    async def test_categories_and_full_detail_on_demand(self) -> None:
        data = await server.get_board(self.ctx, "b1", categories=["done"], detail="completo")
        cols = self._columns(data)
        self.assertEqual(cols["Backlog"]["tasks"], [])
        done = cols["Hecho"]["tasks"]
        self.assertEqual([t["key"] for t in done], ["FNV-5", "FNV-6"])
        self.assertEqual(done[0]["description"], LONG)
        self.assertNotIn("tasks_omitted", cols["Hecho"])

    async def test_limit_per_column(self) -> None:
        cols = self._columns(await server.get_board(self.ctx, "b1", limit_per_column=1))
        self.assertEqual(len(cols["Backlog"]["tasks"]), 1)
        self.assertEqual(cols["Backlog"]["tasks_omitted"], 2)

    async def test_default_is_much_smaller_than_full(self) -> None:
        small = len(json.dumps(await server.get_board(self.ctx, "b1"), ensure_ascii=False))
        full = len(json.dumps(BOARD, ensure_ascii=False))
        self.assertLess(small * 10, full)

    async def test_rejects_unknown_category_or_detail(self) -> None:
        with self.assertRaises(FoundryError):
            await server.get_board(self.ctx, "b1", categories=["hecho"])
        with self.assertRaises(FoundryError):
            await server.get_board(self.ctx, "b1", detail="todo")

    # ── get_task ─────────────────────────────────────────────────────────────
    async def test_get_task_by_id_key_or_number(self) -> None:
        for ref in ("t2", "FNV-2", "fnv-2", "2", "#2"):
            with self.subTest(ref=ref):
                task = await server.get_task(self.ctx, "b1", ref)
                self.assertEqual(task["key"], "FNV-2")
                self.assertEqual(task["description"], LONG)
                self.assertEqual(task["column"], {"id": "c-back", "name": "Backlog", "category": "todo"})

    async def test_get_task_finds_closed_cards_too(self) -> None:
        task = await server.get_task(self.ctx, "b1", "FNV-6")
        self.assertEqual(task["column"]["category"], "done")

    async def test_get_task_wrong_board_key_or_missing(self) -> None:
        for ref in ("ABC-2", "FNV-99", "nope"):
            with self.subTest(ref=ref), self.assertRaises(FoundryError) as err:
                await server.get_task(self.ctx, "b1", ref)
            self.assertIn("search_tasks", str(err.exception))

    # ── search_tasks ───────────────────────────────────────────────────────────
    async def test_find_by_text_ignores_accents_and_skips_done(self) -> None:
        result = await server.search_tasks(self.ctx, "b1", text="busqueda")
        self.assertEqual([t["key"] for t in result["tasks"]], ["FNV-2"])
        with_done = await server.search_tasks(self.ctx, "b1", text="BÚSQUEDA", include_done=True)
        self.assertEqual([t["key"] for t in with_done["tasks"]], ["FNV-2", "FNV-5"])

    async def test_find_by_key_labels_type_and_column(self) -> None:
        by_key = await server.search_tasks(self.ctx, "b1", text="fnv-4")
        self.assertEqual(by_key["tasks"][0]["title"], "Arreglar el hook")
        by_labels = await server.search_tasks(self.ctx, "b1", labels=["Backend", "MCP"])
        self.assertEqual([t["key"] for t in by_labels["tasks"]], ["FNV-4"])
        by_type = await server.search_tasks(self.ctx, "b1", issue_type="historia")
        self.assertEqual([t["key"] for t in by_type["tasks"]], ["FNV-2"])
        in_done = await server.search_tasks(self.ctx, "b1", column="hecho")  # columna pedida: entra
        self.assertEqual(in_done["total"], 2)

    async def test_find_reports_total_beyond_limit(self) -> None:
        result = await server.search_tasks(self.ctx, "b1", limit=1)
        self.assertEqual((result["total"], len(result["tasks"])), (4, 1))
        self.assertEqual(
            set(result["tasks"][0]),
            {"id", "key", "title", "column", "category", "issue_type", "priority", "labels"},
        )
