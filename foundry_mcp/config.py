"""Configuración por entorno (.env local). **Sin credenciales por defecto.**

La autenticación es **interactiva**: la pide Claude Code (elicitation) la primera vez que se
necesita, y se cachea el token en memoria (y, si el usuario acepta, en el gestor de credenciales
del sistema operativo). Aquí solo vive la **config no secreta** (URL de la API, timeout e identidad
de agente).

Las variables `FOUNDRY_TOKEN` / `FOUNDRY_EMAIL` / `FOUNDRY_PASSWORD` siguen existiendo como
**override opcional** para automatización/CI (sin diálogo); si no están, la auth es interactiva.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Carga el .env de al lado (si existe), sin depender del CWD con que arranque el proceso.
# override=True: un .env PERSONAL (gitignoreado) gana sobre los defaults que el `.mcp.json` de
# proyecto inyecta por env (p. ej. FOUNDRY_API_BASE). Así el equipo comparte un default y cada
# quien lo ajusta a su backend (p. ej. local) sin tocar el repo.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

# A dónde habla el MCP si nadie dice lo contrario: el Foundry público. Quien **instala** el
# paquete no tiene un backend en su máquina, y un default a `localhost` le regala un error de
# conexión en la primera herramienta que use, sin nada que explique por qué. Desarrollar contra un
# backend local es la excepción y se pide a propósito con `FOUNDRY_API_BASE`.
DEFAULT_API_BASE = "https://foundry.collie.digital/api/v1"


@dataclass(frozen=True)
class Config:
    api_base: str  # ej. https://foundry.collie.digital/api/v1 (incluye el prefijo /api/v1)
    timeout: float  # s de espera a la API
    # Overrides opcionales para automatización/CI (por defecto vacíos → auth interactiva).
    env_token: str
    env_email: str
    env_password: str
    # `auto` usa clientInfo de la sesión MCP; un valor explícito corrige clientes genéricos.
    agent_source: str
    # MCP no estandariza el modelo; se declara aquí o por llamada de herramienta.
    agent_model: str


def load_config() -> Config:
    # Algunos clientes ya exponen el modelo en su entorno; FOUNDRY_AGENT_MODEL manda cuando se
    # necesita corregirlo. El override por llamada de herramienta sigue teniendo mayor prioridad.
    detected_model = next(
        (
            value
            for name in (
                "FOUNDRY_AGENT_MODEL",
                "CODEX_MODEL",
                "CLAUDE_MODEL",
                "ANTHROPIC_MODEL",
                "OPENCODE_MODEL",
                "GEMINI_MODEL",
            )
            if (value := os.getenv(name, "").strip())
        ),
        "",
    )
    return Config(
        api_base=os.getenv("FOUNDRY_API_BASE", DEFAULT_API_BASE).rstrip("/"),
        timeout=float(os.getenv("FOUNDRY_TIMEOUT", "30")),
        env_token=os.getenv("FOUNDRY_TOKEN", "").strip(),
        env_email=os.getenv("FOUNDRY_EMAIL", "").strip(),
        env_password=os.getenv("FOUNDRY_PASSWORD", ""),
        agent_source=os.getenv("FOUNDRY_AGENT_SOURCE", "auto").strip() or "auto",
        agent_model=detected_model,
    )
