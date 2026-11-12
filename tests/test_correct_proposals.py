"""Corregir lo ya aceptado desde el MCP (FNV-325), y las entidades HTML que se colaban.

En app-pliegue, un criterio y una decisión quedaron guardados con `&lt;slug&gt;` porque la IA
escribió entidades HTML, y una vez aceptados nadie los podía corregir. La regla, elegida por el
usuario: la IA no reescribe lo revisado; propone uno nuevo que lo sustituye (`supersedes`).
"""

from __future__ import annotations

from typing import Any
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from foundry_mcp import server
from foundry_mcp.foundry import FoundryClient


def _cliente() -> AsyncMock:
    client = AsyncMock(spec=FoundryClient)
    for name in (
        "add_task_criterion",
        "update_task_criterion",
        "retire_task_criterion",
        "add_task_decision",
        "edit_task_decision",
        "retire_task_decision",
        "add_task_comment",
    ):
        getattr(client, name).return_value = {"id": "x1"}
    return client


class EntityTests(TestCase):
    def test_converts_entities_outside_code(self) -> None:
        text, changed = server._unescape_entities("La URL lleva &lt;slug&gt; &amp; más")
        self.assertEqual(text, "La URL lleva <slug> & más")
        self.assertTrue(changed)

    def test_keeps_entities_inside_code(self) -> None:
        # Así cuenta su propio bug la tarjeta FNV-325: ahí la entidad es justo lo que se describe.
        original = "Guardó `&lt;slug&gt;` tal cual.\n```\n&amp;nbsp;\n```\ny fuera &lt;b&gt;"
        text, changed = server._unescape_entities(original)
        self.assertEqual(text, "Guardó `&lt;slug&gt;` tal cual.\n```\n&amp;nbsp;\n```\ny fuera <b>")
        self.assertTrue(changed)

    def test_text_without_entities_is_untouched(self) -> None:
        self.assertEqual(server._unescape_entities("a < b && c > d"), ("a < b && c > d", False))
        self.assertEqual(server._unescape_entities("solo `&lt;` en código"), ("solo `&lt;` en código", False))


class CorrectProposalTests(IsolatedAsyncioTestCase):
    async def _call(self, tool: Any, *args: Any, **kwargs: Any) -> tuple[Any, AsyncMock]:
        client = _cliente()
        with (
            patch("foundry_mcp.server._prepare", return_value=client),
            patch("foundry_mcp.server._agent_source", return_value="claude_code"),
        ):
            result = await tool(object(), *args, **kwargs)  # type: ignore[arg-type]
        return result, client

    async def test_proposing_a_replacement_sends_what_it_supersedes(self) -> None:
        result, client = await self._call(
            server.propose_criterion, "ini", "t1", "Comentar pide cuenta", supersedes="c-viejo"
        )
        client.add_task_criterion.assert_awaited_once_with(
            "ini", "t1", "Comentar pide cuenta", supersedes_id="c-viejo"
        )
        self.assertEqual(result, {"id": "x1"})  # sin entidades, sin aviso

    async def test_a_criterion_with_entities_is_saved_with_characters_and_says_so(self) -> None:
        result, client = await self._call(
            server.propose_criterion, "ini", "t1", "La URL lleva &lt;usuario&gt;"
        )
        client.add_task_criterion.assert_awaited_once_with(
            "ini", "t1", "La URL lleva <usuario>", supersedes_id=None
        )
        self.assertEqual(result["aviso"], server.ENTITY_WARNING)

    async def test_a_decision_replacement_and_its_entities(self) -> None:
        result, client = await self._call(
            server.record_decision,
            "ini",
            "t1",
            "Con moderación",
            rationale="Spam &amp; abuso",
            supersedes="d-vieja",
        )
        client.add_task_decision.assert_awaited_once_with(
            "ini", "t1", "Con moderación", "Spam & abuso", repo_link_id=None, supersedes_id="d-vieja"
        )
        self.assertIn("aviso", result)

    async def test_editing_goes_through_the_backend_gate(self) -> None:
        _, client = await self._call(server.edit_criterion, "ini", "t1", "c1", "Pasa CI")
        client.update_task_criterion.assert_awaited_once_with("ini", "t1", "c1", "Pasa CI")
        _, client = await self._call(server.edit_decision, "ini", "t1", "d1", "Usar B", "porque")
        client.edit_task_decision.assert_awaited_once_with("ini", "t1", "d1", "Usar B", "porque")

    async def test_retiring_without_replacement_sends_none(self) -> None:
        _, client = await self._call(server.retire_criterion, "ini", "t1", "c1")
        client.retire_task_criterion.assert_awaited_once_with("ini", "t1", "c1", None)
        _, client = await self._call(server.retire_decision, "ini", "t1", "d1", superseded_by="d2")
        client.retire_task_decision.assert_awaited_once_with("ini", "t1", "d1", "d2")

    async def test_a_comment_with_entities_is_converted_and_warned(self) -> None:
        result, client = await self._call(
            server.add_task_comment, "ini", "t1", "Fe de erratas: era &lt;slug&gt;, no `&lt;x&gt;`"
        )
        sent = client.add_task_comment.await_args.args[2]
        self.assertEqual(sent, "Fe de erratas: era <slug>, no `&lt;x&gt;`")
        self.assertEqual(result["aviso"], server.ENTITY_WARNING)


class RegistryTests(TestCase):
    def test_new_tools_are_in_the_boards_group_with_a_title(self) -> None:
        for name in ("edit_criterion", "edit_decision", "retire_criterion", "retire_decision"):
            self.assertIn(name, server.TOOLSETS["tableros"])
            self.assertIn(name, server.TITLES)

    def test_retiring_is_a_human_gate(self) -> None:
        for tool in (server.retire_criterion, server.retire_decision):
            doc = (tool.__doc__ or "").lower()
            self.assertIn("gate humano", doc)
            self.assertIn("solo si te lo piden", doc)
