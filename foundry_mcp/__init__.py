"""MCP de administración de tableros (Kanban) de Foundry para Claude Code.

Servidor MCP por *stdio* que expone herramientas para listar/crear tableros y gestionar sus
tarjetas contra la API REST de Foundry, actuando como una cuenta de usuario (JWT). No forma
parte del backend ni lo modifica: habla el mismo contrato público que el frontend.
"""

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

# La versión vive en un solo sitio, `pyproject.toml`, que es el que comprueba el workflow de
# publicación contra la etiqueta. Copiarla aquí a mano ya se desincronizó una vez (0.1.0 frente a
# 0.1.1), y esta es la versión que el servidor anuncia a los clientes (FNV-315).
try:
    __version__ = version("foundry-boards-mcp")
except PackageNotFoundError:  # código suelto, sin instalar: no hay metadatos que leer
    __version__ = "0.0.0"
