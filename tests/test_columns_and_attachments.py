"""FNV-265/266: la entrada se elige por categoría; los adjuntos se suben y se borran."""

from __future__ import annotations

import base64
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

import httpx
from mcp.server.elicitation import AcceptedElicitation, CancelledElicitation, DeclinedElicitation
from mcp.server.mcpserver import Elicit

from foundry_mcp import server
from foundry_mcp.foundry import FoundryError, _board_body, _error_message
from foundry_mcp.server import (
    ConfirmForm,
    _attachment_source,
    _Borrado,
    _default_column_id,
    _resolve_column,
    _tamano_legible,
)

# Dobles del `Context` para los resolvers: uno cuyo cliente sabe preguntar y otro que no.
PREGUNTA: Any = SimpleNamespace(client_capabilities=SimpleNamespace(elicitation=SimpleNamespace()))
NO_PREGUNTA: Any = SimpleNamespace(client_capabilities=None)
# La respuesta de una persona que dice que sí.
SI: Any = AcceptedElicitation(data=ConfirmForm(confirmar=True))


def _col(id_: str, name: str, category: str | None = None) -> dict[str, object]:
    return {"id": id_, "name": name, "category": category}


class DefaultColumnTests(TestCase):
    """La columna donde nace una tarjeta sin `column`: la primera `todo`, se llame como se llame."""

    def test_entry_is_the_first_todo_whatever_its_name(self) -> None:
        columnas = [
            _col("c1", "Ideas", "todo"),
            _col("c2", "Cocinando", "in_progress"),
            _col("c3", "Servido", "done"),
        ]
        self.assertEqual(_default_column_id(columnas), "c1")

    def test_a_column_named_backlog_no_longer_wins(self) -> None:
        """El bug de FNV-265: «Backlog» ganaba por el nombre aunque no fuera la entrada."""
        columnas = [
            _col("c1", "Ideas", "todo"),
            _col("c2", "Backlog", "in_progress"),  # se llama así, pero no es la entrada
            _col("c3", "Hecho", "done"),
        ]
        self.assertEqual(_default_column_id(columnas), "c1")

    def test_falls_back_to_the_first_column_without_categories(self) -> None:
        # Datos anteriores al rediseño: sin categoría no hay nada mejor que la primera.
        self.assertEqual(_default_column_id([_col("c1", "Una"), _col("c2", "Otra")]), "c1")

    def test_empty_board_is_an_error(self) -> None:
        with self.assertRaises(FoundryError):
            _default_column_id([])

    def test_resolving_by_name_still_tolerates_accents_and_case(self) -> None:
        columnas = [_col("c1", "En revisión", "in_progress"), _col("c2", "Hecho", "done")]
        self.assertEqual(_resolve_column(columnas, "en revision"), "c1")
        self.assertEqual(_resolve_column(columnas, "c2"), "c2")  # por id
        with self.assertRaises(FoundryError):
            _resolve_column(columnas, "No existe")


class BoardBodyTests(TestCase):
    """El alta de tablero solo manda lo que se eligió (FNV-261)."""

    def test_only_what_was_chosen_travels(self) -> None:
        self.assertEqual(_board_body("T", None, None), {"title": "T"})
        solo_preset = _board_body("T", "generic_es", None)
        self.assertEqual(solo_preset, {"title": "T", "preset": "generic_es"})
        columnas = [{"name": "Ideas", "category": "todo"}]
        cuerpo = _board_body("T", "scrum", columnas)
        self.assertEqual(cuerpo, {"title": "T", "preset": "scrum", "columns": columnas})


class AttachmentSourceTests(TestCase):
    """De dónde salen los bytes que se suben (FNV-266): ruta, texto o base64, y solo una."""

    def test_text_needs_a_filename_and_is_utf8(self) -> None:
        data, name = _attachment_source("", "informe.md", "# Título con acentué", "")
        self.assertEqual(name, "informe.md")
        self.assertEqual(data.decode("utf-8"), "# Título con acentué")
        with self.assertRaises(FoundryError):
            _attachment_source("", "", "contenido sin nombre", "")

    def test_base64_is_decoded_and_bad_input_is_explained(self) -> None:
        data, name = _attachment_source("", "logo.png", "", base64.b64encode(b"\x89PNG").decode())
        self.assertEqual((data, name), (b"\x89PNG", "logo.png"))
        with self.assertRaises(FoundryError):
            _attachment_source("", "logo.png", "", "esto no es base64 !!")

    def test_path_reads_the_file_and_takes_its_name(self) -> None:
        with TemporaryDirectory() as tmp:
            f = Path(tmp) / "captura.txt"
            f.write_bytes(b"hola")
            self.assertEqual(_attachment_source(str(f), "", "", ""), (b"hola", "captura.txt"))
            # `filename` puede renombrarlo al subirlo.
            self.assertEqual(_attachment_source(str(f), "otro.txt", "", "")[1], "otro.txt")
            with self.assertRaises(FoundryError):
                _attachment_source(str(Path(tmp) / "no-existe.txt"), "", "", "")

    def test_exactly_one_source_is_required(self) -> None:
        with self.assertRaises(FoundryError):  # ninguna
            _attachment_source("", "x.txt", "", "")
        with self.assertRaises(FoundryError):  # dos a la vez
            _attachment_source("/tmp/x.txt", "x.txt", "contenido", "")


class ColumnToolsTests(IsolatedAsyncioTestCase):
    """Las herramientas que configuran columnas (FNV-267): lo que explican antes de fallar."""

    def _cliente(self, columnas: list[dict[str, object]]) -> AsyncMock:
        cliente = AsyncMock()
        cliente.get_initiative.return_value = {"board": {"columns": columnas}}
        return cliente

    async def test_deleting_a_column_with_cards_asks_where_they_go(self) -> None:
        columnas = [
            {"id": "c1", "name": "Ideas", "category": "todo", "tasks": []},
            {"id": "c2", "name": "En curso", "category": "in_progress", "tasks": [{"id": "t1"}]},
            {"id": "c3", "name": "Hecho", "category": "done", "tasks": []},
        ]
        cliente = self._cliente(columnas)
        with patch("foundry_mcp.server._prepare", return_value=cliente):
            # Lo que se va a borrar se lee antes de preguntar (resolver): ahí se explica el fallo.
            with self.assertRaises(FoundryError) as caso:
                await server._borrado_columna("ini", "En curso", "", NO_PREGUNTA)
            mensaje = str(caso.exception)
            self.assertIn("1 tarjeta", mensaje)  # dice CUÁNTAS hay que reubicar
            self.assertIn("Ideas", mensaje)  # y a dónde podrían ir
            cliente.delete_column.assert_not_awaited()  # no se llegó a borrar nada

            # Con destino sí procede, y cuenta qué se movió.
            borrado = await server._borrado_columna("ini", "En curso", "Ideas", NO_PREGUNTA)
            resultado = await server.delete_column(NO_PREGUNTA, "ini", "En curso", borrado, SI, "Ideas")
            cliente.delete_column.assert_awaited_once_with("ini", "c2", "c1")
            self.assertEqual((resultado["deleted"], resultado["moved_tasks"]), ("En curso", 1))
            self.assertEqual(resultado["moved_to"], "Ideas")

    async def test_a_column_cannot_be_reassigned_to_itself(self) -> None:
        columnas = [
            {"id": "c1", "name": "Ideas", "category": "todo", "tasks": [{"id": "t1"}]},
            {"id": "c2", "name": "Hecho", "category": "done", "tasks": []},
        ]
        cliente = self._cliente(columnas)
        with patch("foundry_mcp.server._prepare", return_value=cliente):
            with self.assertRaises(FoundryError):
                await server._borrado_columna("ini", "Ideas", "Ideas", NO_PREGUNTA)
            cliente.delete_column.assert_not_awaited()

    async def test_reordering_demands_every_column_and_names_the_missing_ones(self) -> None:
        columnas = [
            {"id": "c1", "name": "Ideas", "category": "todo", "tasks": []},
            {"id": "c2", "name": "En curso", "category": "in_progress", "tasks": []},
            {"id": "c3", "name": "Hecho", "category": "done", "tasks": []},
        ]
        cliente = self._cliente(columnas)
        with patch("foundry_mcp.server._prepare", return_value=cliente):
            with self.assertRaises(FoundryError) as caso:
                await server.reorder_columns(None, "ini", ["Hecho", "Ideas"])
            self.assertIn("En curso", str(caso.exception))  # dice cuál falta
            cliente.reorder_columns.assert_not_awaited()

            await server.reorder_columns(None, "ini", ["Hecho", "En curso", "Ideas"])
            cliente.reorder_columns.assert_awaited_once_with("ini", ["c3", "c2", "c1"])

    async def test_category_is_validated_before_calling(self) -> None:
        cliente = self._cliente([{"id": "c1", "name": "Ideas", "category": "todo", "tasks": []}])
        with patch("foundry_mcp.server._prepare", return_value=cliente):
            with self.assertRaises(FoundryError):
                await server.add_column(None, "ini", "Bloqueadas", "en_curso")  # no existe
            cliente.add_column.assert_not_awaited()


class ToolAnnotationsTests(IsolatedAsyncioTestCase):
    """FNV-268: toda herramienta declara qué clase de operación es, y coherente con su nombre.

    Es la red para lo que vendrá después: una herramienta nueva sin declarar suspende aquí, en vez
    de llegar al cliente como «destructiva» por omisión y enseñar a aprobar sin mirar.
    """

    async def test_every_tool_declares_what_it_does(self) -> None:
        tools = await server.mcp.list_tools()
        self.assertEqual([t.name for t in tools if not t.annotations], [])

    async def test_the_declaration_matches_the_name(self) -> None:
        for t in await server.mcp.list_tools():
            with self.subTest(tool=t.name):
                if t.name.startswith(("list_", "get_", "read_", "search_")) or t.name == "whoami":
                    self.assertIs(t.annotations.read_only_hint, True)
                elif t.name.startswith(("delete_", "unlink_")):
                    self.assertIs(t.annotations.destructive_hint, True)
                else:
                    # Crear o actualizar NO destruye: decirlo es lo que evita que el cliente lo
                    # trate como un borrado (lo no declarado se asume destructivo).
                    self.assertIs(t.annotations.read_only_hint, False)
                    self.assertIs(t.annotations.destructive_hint, False)


class ConfirmBeforeDeletingTests(IsolatedAsyncioTestCase):
    """FNV-269: lo irreversible se pregunta con el dato delante, sin bloquear a quien no puede.

    Desde FNV-310 la pregunta la hace un resolver antes del cuerpo (el SDK la manda por el canal de
    cada protocolo), así que se prueban por separado: qué se pregunta y qué hace el cuerpo con la
    respuesta. El recorrido entero por stdio, en los dos protocolos, va en la verificación.
    """

    def _tablero(self) -> dict[str, object]:
        tarjeta = {
            "id": "t1",
            "title": "La tarjeta vieja",
            "comment_count": 12,
            "attachment_count": 3,
            "tests_total": 0,
        }
        return {"board": {"columns": [{"id": "c1", "name": "Ideas", "tasks": [tarjeta]}]}}

    async def _pregunta_tarjeta(self, cliente: AsyncMock) -> tuple[Any, str]:
        with patch("foundry_mcp.server._prepare", return_value=cliente):
            borrado = await server._borrado_tarjeta("ini", "t1", PREGUNTA)
            pregunta = await server._confirma_tarjeta(borrado, PREGUNTA)
        assert isinstance(pregunta, Elicit)
        return borrado, pregunta.message

    async def test_it_says_what_is_lost_and_deletes_when_accepted(self) -> None:
        cliente = AsyncMock()
        cliente.get_initiative.return_value = self._tablero()
        borrado, pregunta = await self._pregunta_tarjeta(cliente)
        self.assertIn("La tarjeta vieja", pregunta)
        self.assertIn("12 comentarios", pregunta)  # el dato delante, no un «¿seguro?»
        self.assertIn("3 adjuntos", pregunta)
        with patch("foundry_mcp.server._prepare", return_value=cliente):
            resultado = await server.delete_task(PREGUNTA, "ini", "t1", borrado, SI)
        cliente.delete_task.assert_awaited_once_with("ini", "t1")
        self.assertEqual(resultado["status"], "deleted")
        self.assertEqual(resultado["era"], "La tarjeta vieja")

    async def test_cancelling_stops_everything(self) -> None:
        cliente = AsyncMock()
        cliente.get_initiative.return_value = self._tablero()
        borrado, _ = await self._pregunta_tarjeta(cliente)
        respuestas: tuple[Any, ...] = (
            CancelledElicitation(),
            DeclinedElicitation(),
            AcceptedElicitation(data=ConfirmForm(confirmar=False)),
        )
        with patch("foundry_mcp.server._prepare", return_value=cliente):
            for respuesta in respuestas:
                with self.subTest(respuesta=respuesta.action), self.assertRaises(FoundryError):
                    await server.delete_task(PREGUNTA, "ini", "t1", borrado, respuesta)
        cliente.delete_task.assert_not_awaited()

    async def test_a_note_is_named_by_its_title_not_its_uuid(self) -> None:
        cliente = AsyncMock()
        cliente.get_document.return_value = {  # el detalle viene envuelto
            "document": {"id": "n1", "title": "Acta del lunes"},
            "blocks": [],
        }
        cliente.document_backlinks.return_value = [{"id": "n2"}]
        with patch("foundry_mcp.server._prepare", return_value=cliente):
            borrado = await server._borrado_nota("n1", PREGUNTA)
            pregunta = await server._confirma_nota(borrado, PREGUNTA)
            respuesta = await server.delete_note(PREGUNTA, "n1", borrado, SI)
        assert isinstance(pregunta, Elicit)
        self.assertIn("Acta del lunes", pregunta.message)
        self.assertNotIn("n1", pregunta.message)  # nadie reconoce una nota por su uuid
        self.assertIn("enlazada desde 1 nota", pregunta.message)
        self.assertEqual(respuesta, "Nota «Acta del lunes» borrada.")

    async def test_the_question_agrees_with_what_is_being_deleted(self) -> None:
        """«La tarjeta … ¿Lo borro?» delataba la plantilla; el artículo manda."""
        cliente = AsyncMock()
        cliente.get_initiative.return_value = self._tablero()
        _, pregunta = await self._pregunta_tarjeta(cliente)
        self.assertTrue(pregunta.endswith("¿La borro?"))

        adjunto = _Borrado("El adjunto «acta.md» (94 B) de esta tarjeta.", {})
        masculino = server._preguntar_borrado(adjunto, PREGUNTA)
        assert isinstance(masculino, Elicit)
        self.assertTrue(masculino.message.endswith("¿Lo borro?"))

    async def test_the_list_of_losses_reads_like_a_sentence(self) -> None:
        """Enumerar con comas y una «y» final; no «a y b y c»."""
        tablero = self._tablero()
        tablero["board"]["columns"][0]["tasks"][0]["tests_total"] = 2  # type: ignore[index]
        cliente = AsyncMock()
        cliente.get_initiative.return_value = tablero
        _, pregunta = await self._pregunta_tarjeta(cliente)
        self.assertIn("con 12 comentarios, 3 adjuntos y 2 casos de prueba.", pregunta)

    async def test_a_client_that_cannot_ask_does_not_block_the_work(self) -> None:
        """Por el SDK de verdad (resolvers incluidos), sin nadie a quien preguntar."""
        cliente = AsyncMock()
        cliente.get_initiative.return_value = self._tablero()
        with patch("foundry_mcp.server._prepare", return_value=cliente):
            resultado = await server.mcp.call_tool("delete_task", {"initiative_id": "ini", "task_id": "t1"})
        self.assertFalse(resultado.is_error)
        cliente.delete_task.assert_awaited_once_with("ini", "t1")


class PromptTests(IsolatedAsyncioTestCase):
    """FNV-270: los ritos del tablero, publicados para que el usuario los invoque."""

    async def test_the_three_rites_are_published_with_their_arguments(self) -> None:
        publicados = await server.mcp.list_prompts()
        prompts = {p.name: [a.name for a in (p.arguments or [])] for p in publicados}
        self.assertEqual(
            prompts,
            {
                "trabajar_tarjeta": ["initiative_id", "task_id"],
                "dejar_avance": ["initiative_id", "task_id"],
                "repasar_tablero": ["initiative_id"],
            },
        )

    async def test_they_render_with_the_ids_and_say_what_is_not_negotiable(self) -> None:
        r = await server.mcp.get_prompt(
            "trabajar_tarjeta", {"initiative_id": "INI", "task_id": "T1"}
        )
        texto = r.messages[0].content.text
        self.assertIn("T1", texto)
        self.assertIn("INI", texto)
        # Lo que no puede perderse por el camino: el gate es humano.
        self.assertIn("Hecho lo decide una persona", texto)

    async def test_the_review_prompt_does_not_touch_anything(self) -> None:
        r = await server.mcp.get_prompt("repasar_tablero", {"initiative_id": "INI"})
        self.assertIn("No muevas", r.messages[0].content.text)


class ErrorMessageTests(TestCase):
    """Un error tiene que decir QUÉ regla se rompió: el motivo viaja en dos formatos distintos."""

    def test_a_domain_rule_says_which_rule_broke(self) -> None:
        # El handler de DomainError del backend responde así; mirar solo `detail` daba «400: None».
        respuesta = httpx.Response(
            400,
            json={"error": {"code": "invalid_operation", "message": "Faltan columnas"}},
        )
        self.assertIn("Faltan columnas", _error_message(respuesta))

    def test_the_fastapi_shape_still_works(self) -> None:
        respuesta = httpx.Response(404, json={"detail": "La iniciativa no existe"})
        mensaje = _error_message(respuesta)
        self.assertIn("La iniciativa no existe", mensaje)
        self.assertIn("no encontrado", mensaje)  # la pista sigue puesta

    def test_a_body_with_no_reason_never_says_none(self) -> None:
        self.assertNotIn("None", _error_message(httpx.Response(500, text="")))


class ReadableSizeTests(TestCase):
    """Lo que se le enseña a quien va a borrar: un tamaño de verdad, no «0 KB»."""

    def test_it_uses_the_unit_that_fits(self) -> None:
        self.assertEqual(_tamano_legible(94), "94 B")
        self.assertEqual(_tamano_legible(12_288), "12 KB")
        self.assertEqual(_tamano_legible(11_534_336), "11.0 MB")


class DeleteColumnWordingTests(IsolatedAsyncioTestCase):
    """La pregunta la lee una persona: una tarjeta es «su tarjeta», no «sus 1 tarjeta»."""

    async def _pregunta(self, cuantas: int) -> str:
        columnas = [
            {"id": "c1", "name": "Ideas", "category": "todo", "tasks": []},
            {
                "id": "c2",
                "name": "En curso",
                "category": "in_progress",
                "tasks": [{"id": f"t{i}"} for i in range(cuantas)],
            },
            {"id": "c3", "name": "Hecho", "category": "done", "tasks": []},
        ]
        cliente = AsyncMock()
        cliente.get_initiative.return_value = {"board": {"columns": columnas}}
        with patch("foundry_mcp.server._prepare", return_value=cliente):
            borrado = await server._borrado_columna("ini", "En curso", "Ideas", PREGUNTA)
            pregunta = await server._confirma_columna(borrado, PREGUNTA)
        assert isinstance(pregunta, Elicit)
        return pregunta.message

    async def test_one_card_reads_as_singular(self) -> None:
        pregunta = await self._pregunta(1)
        self.assertIn("su tarjeta se muda a «Ideas»", pregunta)

    async def test_several_cards_are_counted(self) -> None:
        self.assertIn("sus 3 tarjetas se mudan a «Ideas»", await self._pregunta(3))
