"""WebSocket API facade.

WS endpoint живёт в `aria.routers.system`. Этот модуль — совместимая
точка входа для внешних импортёров.
"""

from aria.routers.system import websocket_endpoint

__all__ = ["websocket_endpoint"]
