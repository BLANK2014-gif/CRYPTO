"""
Roostoo Trading Bot — Web Server (Dashboard Backend)
======================================================
FastAPI + WebSocket telemetry backend.
Runs in a dedicated thread — never blocks the trading loop.

Endpoints:
  GET  /api/status          — Full telemetry JSON
  WS   /ws                  — Real-time WebSocket telemetry
  POST /command/pause       — Pause trading
  POST /command/kill        — Emergency kill switch
  POST /command/reconcile   — Force reconciliation
  POST /command/reset_risk  — Reset risk freeze
  POST /command/derisk      — Market-sell exposure
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Set

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends, Header
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

logger = logging.getLogger("web_server")


class DashboardServer:
    """
    FastAPI dashboard server running in a dedicated thread.
    Provides REST API, WebSocket telemetry, and serves the frontend.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        auth_token: str = "",
        ws_interval: float = 1.0,
    ):
        self._host = host
        self._port = port
        self._auth_token = auth_token
        self._ws_interval = ws_interval
        self._app = FastAPI(title="Roostoo Trading Bot", docs_url=None, redoc_url=None)
        self._thread: Optional[threading.Thread] = None
        self._running = False

        # State provider function (set by main.py)
        self._telemetry_provider: Optional[Callable[[], Dict[str, Any]]] = None
        self._command_handler: Optional[Callable[[str, Dict], Dict]] = None

        # WebSocket clients
        self._ws_clients: Set[WebSocket] = set()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dashboard")

        # Setup routes
        self._setup_routes()

    def set_telemetry_provider(self, provider: Callable[[], Dict[str, Any]]) -> None:
        """Set the function that returns current telemetry state."""
        self._telemetry_provider = provider

    def set_command_handler(self, handler: Callable[[str, Dict], Dict]) -> None:
        """Set the function that handles operator commands."""
        self._command_handler = handler

    # =======================================================================
    # Authentication
    # =======================================================================

    def _verify_auth(self, authorization: Optional[str] = None) -> bool:
        """Verify bearer token."""
        if not self._auth_token:
            return True  # No auth configured
        if not authorization:
            return False
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1] == self._auth_token
        return authorization == self._auth_token

    # =======================================================================
    # Routes
    # =======================================================================

    def _setup_routes(self):
        app = self._app

        @app.get("/")
        async def index():
            """Serve the dashboard frontend."""
            import pathlib
            html_path = pathlib.Path(__file__).parent.parent / "web" / "index.html"
            if html_path.exists():
                return FileResponse(str(html_path), media_type="text/html")
            return HTMLResponse("<h1>Roostoo Trading Bot — Dashboard not found</h1>")

        @app.get("/api/status")
        async def api_status(authorization: Optional[str] = Header(None)):
            """Return full telemetry JSON."""
            if not self._verify_auth(authorization):
                raise HTTPException(status_code=401, detail="Unauthorized")
            if self._telemetry_provider:
                data = await asyncio.get_event_loop().run_in_executor(
                    self._executor, self._telemetry_provider,
                )
                return JSONResponse(content=data)
            return JSONResponse(content={"error": "No telemetry provider"})

        @app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            """WebSocket telemetry stream (~1 update/second)."""
            # Auth check via query param or first message
            token = websocket.query_params.get("token", "")
            if self._auth_token and token != self._auth_token:
                await websocket.close(code=1008, reason="Unauthorized")
                return

            await websocket.accept()
            self._ws_clients.add(websocket)
            logger.info("WebSocket client connected — total=%d", len(self._ws_clients))

            try:
                while True:
                    # Send telemetry
                    if self._telemetry_provider:
                        try:
                            data = await asyncio.get_event_loop().run_in_executor(
                                self._executor, self._telemetry_provider,
                            )
                            await websocket.send_json(data)
                        except Exception as exc:
                            logger.debug("WS send error: %s", exc)
                            break

                    await asyncio.sleep(self._ws_interval)

                    # Check for incoming messages (heartbeat / commands)
                    try:
                        msg = await asyncio.wait_for(
                            websocket.receive_text(), timeout=0.01,
                        )
                        # Process ping
                        if msg == "ping":
                            await websocket.send_text("pong")
                    except asyncio.TimeoutError:
                        pass

            except WebSocketDisconnect:
                pass
            except Exception as exc:
                logger.debug("WebSocket error: %s", exc)
            finally:
                self._ws_clients.discard(websocket)
                logger.info("WebSocket client disconnected — total=%d", len(self._ws_clients))

        # --- Operator commands ---

        @app.post("/command/{command_name}")
        async def execute_command(
            command_name: str,
            authorization: Optional[str] = Header(None),
        ):
            """Execute an operator safety command."""
            if not self._verify_auth(authorization):
                raise HTTPException(status_code=401, detail="Unauthorized")

            valid_commands = {"pause", "kill", "reconcile", "reset_risk", "derisk", "shutdown"}
            if command_name not in valid_commands:
                raise HTTPException(status_code=400, detail=f"Unknown command: {command_name}")

            if not self._command_handler:
                raise HTTPException(status_code=503, detail="Command handler not available")

            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    self._executor,
                    self._command_handler,
                    command_name,
                    {},
                )
                return JSONResponse(content=result)
            except Exception as exc:
                logger.error("Command %s failed: %s", command_name, exc)
                raise HTTPException(status_code=500, detail=str(exc))

        @app.get("/health")
        async def health_check():
            return {"status": "ok", "timestamp": time.time()}

    # =======================================================================
    # Server lifecycle
    # =======================================================================

    def start(self) -> None:
        """Start the dashboard server in a background thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_server,
            name="dashboard-server",
            daemon=True,
        )
        self._thread.start()
        logger.info("Dashboard server starting on %s:%d", self._host, self._port)

    def _run_server(self) -> None:
        """Run uvicorn in a separate thread."""
        try:
            config = uvicorn.Config(
                app=self._app,
                host=self._host,
                port=self._port,
                log_level="warning",
                access_log=False,
            )
            server = uvicorn.Server(config)
            server.run()
        except Exception as exc:
            logger.error("Dashboard server error: %s", exc)
            self._running = False

    def stop(self) -> None:
        """Stop the dashboard server."""
        self._running = False
        logger.info("Dashboard server stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def connected_clients(self) -> int:
        return len(self._ws_clients)
