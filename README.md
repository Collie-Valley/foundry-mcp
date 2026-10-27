# Foundry Boards MCP

[![PyPI](https://img.shields.io/pypi/v/foundry-boards-mcp)](https://pypi.org/project/foundry-boards-mcp/)
[![CI](https://github.com/Collie-Valley/foundry-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Collie-Valley/foundry-mcp/actions/workflows/ci.yml)
[![Licencia: MIT](https://img.shields.io/badge/licencia-MIT-blue)](./LICENSE)

Servidor **MCP** (Model Context Protocol) que deja a **Claude Code, Codex, OpenCode, Gemini** y
otros clientes compatibles trabajar con los **tableros y las notas de
[Foundry](https://foundry.collie.digital)**: leer y mover tarjetas, gestionar épicas, sprints y
etiquetas, dejar avances y verificaciones con su commit, proponer criterios y decisiones para que
una persona los apruebe, y leer y escribir la documentación del Área.

*An MCP server that lets AI coding agents work with Foundry's agile boards and notes.*

```
Agente IA  ──stdio──►  foundry-boards (este MCP)  ──HTTP + JWT──►  Foundry API (/api/v1)
                                                                    (permisos por rol)
```

No toca el backend de Foundry: habla el mismo contrato REST que usa la aplicación web y se
autentica con **tu cuenta**. Los permisos los decide el servidor: lo que tu cuenta no puede hacer
en la web, el agente tampoco puede hacerlo.

## Empezar en un minuto

Necesitas dos cosas:

- **[`uv`](https://docs.astral.sh/uv/getting-started/installation/)** en el PATH. Con él no hace
  falta instalar Python ni el paquete: `uvx` lo baja, lo guarda en su caché y lo ejecuta.
- **Una cuenta de Foundry.** Para editar los tableros de un Área, tienes que ser colaborador o
  dueño de ella.

**Claude Code**

```bash
claude mcp add-json foundry-boards '{"type":"stdio","command":"uvx","args":["foundry-boards-mcp"]}'
```

**Clientes que usan `mcpServers`** (Claude Desktop, Gemini CLI, Cursor…), en su fichero de
configuración. Es lo mismo que trae [`mcp.json.example`](./mcp.json.example):

```json
{
  "mcpServers": {
    "foundry-boards": {
      "command": "uvx",
      "args": ["foundry-boards-mcp"],
      "env": { "FOUNDRY_AGENT_SOURCE": "auto" }
    }
  }
}
```

**Codex**, en `~/.codex/config.toml`:

```toml
[mcp_servers.foundry-boards]
command = "uvx"
args = ["foundry-boards-mcp"]
```

**OpenCode**, en `opencode.json`:

```json
{
  "mcp": {
    "foundry-boards": { "type": "local", "command": ["uvx", "foundry-boards-mcp"], "enabled": true }
  }
}
```

Para comprobarlo, pídele al agente *«usa whoami de foundry-boards»*. La primera vez se abre un
diálogo para entrar con tu correo y tu contraseña; si marcas **«recordar»**, no te lo vuelve a pedir
hasta que caduque la sesión (ver [Autenticación](#autenticación)).

No hay que configurar ningún backend: por omisión habla con el Foundry público. Para fijar una
versión concreta, usa `"args": ["foundry-boards-mcp@0.1.1"]`.

## Qué puedes pedirle

- *«Lista mis tableros y enséñame las tarjetas del de Marketing.»*
- *«Crea la tarea "Migrar auth a OAuth" en En progreso, prioridad alta, y asígnasela a Ana.»*
- *«Pasa a Hecho todas las tarjetas de En revisión que tengan sus casos de prueba en verde.»*
- *«Deja en la FNV-42 el avance con el commit que acabo de hacer y la verificación con los tests.»*
- *«Lee la nota de arquitectura del Área y dime qué decisiones siguen abiertas.»*

## Configuración

Todo es opcional. Se pasa como variable de entorno (el bloque `env` de la configuración del
cliente) o, si trabajas con una copia del código, en un `.env` en su raíz.

| Variable | Para qué |
|---|---|
| `FOUNDRY_API_BASE` | Otro backend: el tuyo en local (`http://localhost:8000/api/v1`) o uno propio. Por omisión, `https://foundry.collie.digital/api/v1`. |
| `FOUNDRY_TIMEOUT` | Segundos de espera a la API (30 por omisión). |
| `FOUNDRY_AGENT_SOURCE` | Qué cliente firma lo que crea el agente. `auto` lo deduce de la sesión MCP; si tu cliente se anuncia con un nombre genérico, fíjalo (`codex`, `claude_code`, `opencode`, `gemini`…). |
| `FOUNDRY_AGENT_MODEL` | El modelo que usa el agente, si es siempre el mismo. Lo que el agente declare en la sesión manda sobre esto. |
| `FOUNDRY_TOOLSETS` | Qué grupos de herramientas exponer, separados por comas: `tableros` (tarjetas, hilo, gates, vínculos), `qa`, `sprints`, `columnas`, `adjuntos`, `notas`. Sin la variable, todos. `login`, `logout` y `whoami` van siempre. Úsalo en clientes que cargan todas las herramientas en el contexto (Codex, los de `mcpServers`): con solo `notas` la lista pesa unos 9 000 caracteres en vez de 47 000. |
| `FOUNDRY_TOKEN`, o `FOUNDRY_EMAIL` + `FOUNDRY_PASSWORD` | Solo para automatización o para clientes sin diálogo: el MCP entra sin preguntar. Para uso normal, no los definas. |

## Identidad del agente

Todo lo que crea el MCP (descripciones, comentarios, avances, verificaciones y cambios del
historial) queda marcado como hecho por una IA, con dos datos aparte:

- **`agent_source`**: qué cliente lo hizo (`claude_code`, `codex`, `opencode`, `gemini`, `other` o
  `unknown`). Sale solo, del nombre con el que se presenta el cliente: Claude Code (`claude-code`),
  Codex (`codex-mcp-client`) y OpenCode (`opencode`) se reconocen sin configurar nada.
- **`agent_model`**: con qué modelo, por ejemplo `claude-opus-5-5` o `gpt-6-luna`.

Foundry lo enseña como `IA · Codex · gpt-6-luna` junto a cada aporte, así que no hace falta firmar
el texto.

**El modelo hay que declararlo.** MCP no tiene un campo para él y ningún cliente se lo pasa a sus
servidores, así que solo lo sabe el agente. Basta con decirlo **una vez**: al empezar, con
`whoami(agent_model="…")`, o con el argumento `agent_model` de cualquier herramienta. El servidor lo
recuerda y lo pone en todo lo que el agente haga durante la sesión, también en las herramientas que
no tienen ese argumento (notas, columnas, adjuntos). Si se cambia de modelo a mitad de sesión, basta
con declararlo de nuevo: manda el último. El prompt `trabajar_tarjeta` ya lo pide como primer paso.

Si el proceso usa siempre el mismo modelo, también puede fijarse con `FOUNDRY_AGENT_MODEL`. Por
compatibilidad se aceptan `CODEX_MODEL`, `CLAUDE_MODEL`, `ANTHROPIC_MODEL`, `OPENCODE_MODEL` y
`GEMINI_MODEL`, pero solo si las defines tú: los clientes no las rellenan.

La identidad la **declara el cliente**: no es una certificación y no da permisos. Lo que puede
hacer el agente lo decide el rol de la cuenta.

## Autenticación

No hay credenciales en ficheros ni en la configuración. La primera herramienta que necesita
sesión, o la tool `login`, abre un **diálogo** en tu cliente que pide correo y contraseña, o un
token.

- La **contraseña no se guarda en ningún sitio**: solo sirve para obtener el token.
- El **token** vive en memoria durante la sesión. Si marcas **«recordar»**, se guarda en el
  gestor de credenciales del sistema (Windows Credential Manager, macOS Keychain o Secret Service),
  nunca en un fichero.
- El token dura **24 horas** y no se renueva solo. Al caducar, el MCP mira primero el gestor de
  credenciales: si otra ventana ya renovó la sesión, la reutiliza sin preguntar. Con varias
  ventanas abiertas, solo la primera pide entrar.
- `login` cambia de cuenta; `logout` olvida el token, en memoria y en el sistema.

El diálogo usa *elicitation* de MCP. Si tu cliente no la soporta, usa `FOUNDRY_TOKEN` o
`FOUNDRY_EMAIL` + `FOUNDRY_PASSWORD` (ver [Configuración](#configuración)).

## Herramientas

74 herramientas y 3 prompts. Cada una declara si **solo lee** (23), si **crea o actualiza** (45) o
si **borra** (6): el cliente lo usa para saber qué puede aprobar solo y dónde pararse. Las que
borran (tarjeta, nota, adjunto, columna) además preguntan antes y dicen qué se va a perder.

**Sesión y panorama**

| Herramienta | Qué hace |
|---|---|
| `login` · `logout` | Entra (abre el diálogo) o cambia de cuenta · olvida el token. |
| `whoami` | La cuenta con la que actúa el MCP, y donde el agente declara su modelo. |
| `list_spaces` | Las Áreas de trabajo que ves. |
| `list_boards` · `list_all_boards` | Tableros personales o de un Área · todos a la vez. |
| `get_board` | Un tablero: sus columnas con sus tarjetas, en resumen y sin las de cierre salvo que las pidas (`categories`, `detail`, `limit_per_column`). |
| `get_task` | Una tarjeta entera, descripción incluida, por su id o su clave (`FNV-308`). |
| `search_tasks` | Buscar tarjetas por texto, etiquetas, columna o tipo. |

**Tableros y columnas**

| Herramienta | Qué hace |
|---|---|
| `list_board_presets` | Las plantillas de columnas entre las que se elige al crear un tablero. |
| `create_board` | Tablero nuevo, personal o de un Área, con una plantilla o columnas a medida. |
| `move_board_to_space` | Lleva un tablero personal a un Área para compartirlo. |
| `add_column` · `rename_column` · `reorder_columns` | Añadir, renombrar (el historial no se toca) y reordenar columnas. |
| `set_column_category` · `set_column_color` | Su tipo (`todo`, `in_progress`, `done`) y su color. |
| `delete_column` | Borra una columna; si tiene tarjetas, dices a dónde se mudan. |

**Tarjetas**

| Herramienta | Qué hace |
|---|---|
| `create_task` · `create_epic` | Tarjeta nueva · épica, la raíz que agrupa historias, tareas y bugs. |
| `edit_task` | Edita título, descripción, prioridad, tipo o responsable (solo lo que pases). |
| `move_task` · `transition_task` | Mueve de columna o de posición · cambia de estado y lo deja en el historial. |
| `set_task_hierarchy` | Tipo y padre: épica → historia → tarea/bug → subtarea. |
| `set_task_details` · `set_task_labels` | Puntos, fechas y bandera · etiquetas por nombre (crea las que falten). |
| `link_tasks` · `get_task_links` · `unlink_tasks` | Vínculos entre tarjetas: `blocks`, `is_blocked_by`, `relates_to`. |
| `link_repo` · `get_repo_links` · `unlink_repo` | Ata la tarjeta a un repositorio, rama, PR o commit. |
| `list_epics` | Las épicas del tablero. |
| `create_tasks_from_seeds` | Convierte ideas del Vivero en tarjetas propuestas. |
| `delete_task` | Borra una tarjeta. Irreversible. |

**Sprints**

| Herramienta | Qué hace |
|---|---|
| `list_sprints` · `create_sprint` | Los sprints del tablero · uno nuevo, planificado. |
| `start_sprint` · `complete_sprint` | Arrancarlo · cerrarlo (lo no terminado vuelve al backlog). |
| `assign_task_to_sprint` | Mete una tarjeta en un sprint, o la devuelve al backlog. |

**El hilo de la tarjeta**

| Herramienta | Qué hace |
|---|---|
| `report_progress` | Un avance: qué se hizo, cuánto queda y en qué commit se ve. |
| `report_verification` | La evidencia de que funciona: `ok`, `fallo` o `parcial`, con su detalle. |
| `flag_blocker` | Marca la tarjeta como bloqueada y explica por qué. |
| `add_task_entry` · `add_task_comment` | Un aporte tipado (avance, bloqueo, pregunta…) · una nota libre. |
| `get_task_comments` · `get_task_history` | El hilo · el historial estructurado (qué cambió, de qué a qué, quién y cuándo). |

**Lo que decide una persona**

La IA **propone** y una persona acepta o descarta, en la web o con estas herramientas. Las de
revisión son la decisión de una persona: un agente no debería aprobar lo que él mismo propuso.

| Herramienta | Qué hace |
|---|---|
| `propose_criterion` · `list_criteria` · `review_criterion` | Criterios de aceptación: proponer, leer su estado, aceptar o descartar. |
| `record_decision` · `list_decisions` · `review_decision` | Decisiones de diseño, que se pueden atar a un commit. |
| `review_task_proposal` | Acepta o rechaza una tarjeta propuesta por la IA. |

**Casos de prueba**

| Herramienta | Qué hace |
|---|---|
| `add_test_case` · `list_test_cases` | Pasos y resultado esperado · su estado (`pending`, `passed`, `failed`). |
| `mark_test_case` · `list_test_executions` | Registra el resultado de una ejecución · la bitácora de ejecuciones. |

**Adjuntos**

| Herramienta | Qué hace |
|---|---|
| `list_task_attachments` · `read_task_attachment` | Lista y lee adjuntos: las imágenes llegan como imagen y el texto como texto. |
| `upload_task_attachment` | Sube un fichero desde el disco, desde un texto o desde base64. |
| `delete_task_attachment` | Borra un adjunto y dice qué se borró. |

**Notas**

| Herramienta | Qué hace |
|---|---|
| `list_notes` · `read_note` · `search_notes` | El árbol de notas del Área · una nota en markdown · buscar por contenido. |
| `create_note` · `create_note_collection` · `write_note` | Crear una nota · una carpeta con su árbol · escribir en una que ya existe. |
| `rename_note` · `move_note` · `delete_note` | Renombrar, mover y borrar. |
| `link_notes` · `get_note_backlinks` | Enlazar dos notas · qué apunta a una nota. |
| `link_note_to_task` · `get_task_notes` | Atar una nota a una tarjeta · qué documentación respalda una tarjeta. |
| `extract_tasks_from_note` | Saca tarjetas de una nota, con la referencia al párrafo del que salen. |

**Prompts**, que lanzas tú desde el cliente como un comando:

| Prompt | Qué hace |
|---|---|
| `trabajar_tarjeta` | El ciclo de una tarjeta: plan visible, avance con su commit, evidencia y entrega. «Hecho» lo decide una persona. |
| `dejar_avance` | Un avance que se entiende dentro de un mes, con su evidencia y su commit. |
| `repasar_tablero` | Qué está atascado, qué espera una decisión y qué se puede cerrar. Solo lee. |

**Detalles pensados para el agente**

- Las columnas se nombran por nombre (sin importar tildes ni mayúsculas) o por id. Cada tablero
  tiene las suyas, así que se leen con `get_board` en vez de suponerlas: lo único estable de una
  columna es su `category`. Una tarjeta creada sin columna nace en la primera de tipo `todo`.
- El responsable se da por nombre o correo en los tableros de un Área. Para quitarlo:
  `edit_task(..., assignee="")`.
- `priority`: `highest`, `high`, `medium`, `low`, `lowest` (también `baja`, `media`, `alta`).
  `issue_type`: `epic`, `historia`, `tarea`, `bug`, `incidencia`, `subtarea`.

## Lo que todavía no hace

- **Borrar un tablero entero**: la web puede, el MCP aún no.
- **Editar el flujo de estados** de un tablero más allá de sus columnas.

Si te hace falta algo, [abre una incidencia](https://github.com/Collie-Valley/foundry-mcp/issues).

## Seguridad

- Los permisos los aplica el **servidor** según el rol de la cuenta; el MCP no puede saltárselos.
- No se guardan contraseñas. El token solo se guarda si lo pides, y en el gestor de credenciales
  del sistema.
- Da a la cuenta que use el agente **solo el acceso que necesite**: invítala como colaboradora
  únicamente a las Áreas que deba tocar.

## Solución de problemas

| Síntoma | Qué pasa |
|---|---|
| El servidor no arranca | `uv` no está en el PATH de tu cliente. Comprueba `uvx --version` en una terminal y reinicia el cliente. |
| Pide entrar cada vez | No marcaste «recordar»: la sesión se queda solo en memoria. |
| *«Este cliente no puede pedir credenciales…»* | Tu cliente no soporta *elicitation*: usa `FOUNDRY_TOKEN` o `FOUNDRY_EMAIL` + `FOUNDRY_PASSWORD`. |
| 403 al cambiar algo | La cuenta es observadora o no es miembro de esa Área. |
| 404 en un tablero o una tarjeta | El id no es correcto, o la cuenta no ve ese tablero. |
| Las tools nuevas no aparecen | Los clientes leen la lista de tools al arrancar: reinícialo. |

## Desarrollo

```bash
git clone https://github.com/Collie-Valley/foundry-mcp.git
cd foundry-mcp
uv sync                 # crea .venv con las dependencias y el grupo dev
uv run pytest -q        # las pruebas
uvx ruff check .        # el estilo, con la configuración de pyproject.toml
uv run mypy             # los tipos, en modo estricto (también desde pyproject.toml)
```

En Windows, `.\run.ps1` hace el `uv sync` y comprueba que el servidor carga y lista sus tools.

Para que tu cliente use **tu copia** en vez del paquete publicado, lánzalo con `uv run` apuntando a
la carpeta (ruta absoluta):

```json
{
  "mcpServers": {
    "foundry-boards": {
      "command": "uv",
      "args": ["run", "--directory", "/ruta/a/foundry-mcp", "python", "run_server.py"]
    }
  }
}
```

Con una copia del código, el MCP lee el `.env` de la raíz del repositorio:
[`.env.example`](./.env.example) dice qué admite. Ese `.env` gana sobre el bloque `env` del cliente, y nunca se versiona.

El estilo se comprueba con `ruff check` y nada más: el formateador automático no se usa en este
proyecto. Los tipos, con `mypy --strict` sobre el paquete (no sobre las pruebas). Las pruebas cubren el cliente HTTP y los ayudantes del servidor con dobles, sin hablar
con un Foundry real.

## Publicar una versión

Se publica desde una etiqueta, nunca a mano. Una versión subida a PyPI no se puede reemplazar.

1. Sube `version` en `pyproject.toml` y haz merge a `main`.
2. `git tag v<versión> && git push origin v<versión>`

El workflow [`publish.yml`](./.github/workflows/publish.yml) comprueba que la etiqueta coincide
con la versión, pasa las pruebas, construye el paquete y lo sube a PyPI por *trusted publishing*
(OIDC), sin ningún token guardado.

## Licencia

[MIT](./LICENSE) © Collie Valley.
