"""MCP de administración de tableros (Kanban) de Foundry para Claude Code.

Servidor MCP por *stdio* que expone herramientas para listar/crear tableros y gestionar sus
tarjetas contra la API REST de Foundry, actuando como una cuenta de usuario (JWT). No forma
parte del backend ni lo modifica: habla el mismo contrato público que el frontend.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
