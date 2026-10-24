"""Graba a archivos los datos crudos de Polymarket para analizarlos después.

Escribe, dentro de una carpeta:
    libro_AAAA-MM-DD.jsonl      cada mensaje del WebSocket de mercado (libro, cambios de precio, operaciones)
    marcador_AAAA-MM-DD.jsonl   cada mensaje del canal de deportes (marcador, periodo, minuto)
    partidos.json               los mercados NBA vigentes (partido, resultado, token_id, hora de inicio)

Cada línea de los .jsonl es {"t": segundos desde 1970 (UTC), "m": mensaje tal como llegó}.
El día del nombre del archivo es UTC.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TextIO

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

SPORTS_WS_URL = "wss://sports-api.polymarket.com/ws"


class Grabador:
    def __init__(self, carpeta: Path, flush_cada: float = 1.0, reloj: Callable[[], float] = time.time):
        self.carpeta = Path(carpeta)
        self.carpeta.mkdir(parents=True, exist_ok=True)
        self.escritos = 0  # líneas escritas desde que arrancó
        self._flush_cada = flush_cada
        self._reloj = reloj
        self._candado = threading.Lock()
        self._abiertos: dict[str, tuple[str, TextIO]] = {}  # tipo -> (día, archivo)
        self._ultimo_flush = time.monotonic()

    def libro(self, mensaje: dict) -> None:
        self._escribir("libro", mensaje)

    def marcador(self, mensaje: dict) -> None:
        self._escribir("marcador", mensaje)

    def partidos(self, mercados: list[dict]) -> None:
        """Reemplaza partidos.json con la lista actual de mercados."""
        datos = {"momento": self._reloj(), "mercados": mercados}
        temporal = self.carpeta / "partidos.json.tmp"
        temporal.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
        temporal.replace(self.carpeta / "partidos.json")

    def cerrar(self) -> None:
        with self._candado:
            for _, archivo in self._abiertos.values():
                archivo.close()
            self._abiertos.clear()

    def _escribir(self, tipo: str, mensaje: dict) -> None:
        ahora = self._reloj()
        dia = datetime.fromtimestamp(ahora, timezone.utc).strftime("%Y-%m-%d")
        linea = json.dumps({"t": round(ahora, 3), "m": mensaje}, separators=(",", ":"), ensure_ascii=False)
        with self._candado:
            actual = self._abiertos.get(tipo)
            if actual is None or actual[0] != dia:
                if actual is not None:
                    actual[1].close()
                archivo = (self.carpeta / f"{tipo}_{dia}.jsonl").open("a", encoding="utf-8")
                self._abiertos[tipo] = (dia, archivo)
            else:
                archivo = actual[1]
            archivo.write(linea + "\n")
            self.escritos += 1
            if time.monotonic() - self._ultimo_flush >= self._flush_cada:
                for _, abierto in self._abiertos.values():
                    abierto.flush()
                self._ultimo_flush = time.monotonic()


class FuenteMarcador:
    """Escucha el canal de deportes y graba los mensajes de la NBA.

    El canal no pide suscripción: al conectarte manda todos los partidos activos.
    El servidor manda "ping" cada pocos segundos y hay que contestar "pong".
    """

    def __init__(self, grabador: Grabador, url: str = SPORTS_WS_URL):
        self.grabador = grabador
        self.url = url
        self.estado = "conectando…"

    def iniciar(self) -> None:
        threading.Thread(target=self._bucle, daemon=True).start()

    def _bucle(self) -> None:
        espera = 1
        while True:
            try:
                with connect(self.url, open_timeout=15, ping_interval=None) as ws:
                    self.estado = "en vivo"
                    espera = 1
                    while True:
                        try:
                            texto = ws.recv(timeout=1)
                        except TimeoutError:
                            continue
                        if isinstance(texto, str) and texto.strip().lower() == "ping":
                            ws.send("pong")
                        else:
                            self._procesar(texto)
            except (OSError, WebSocketException) as e:
                self.estado = f"reconectando ({e.__class__.__name__})…"
                time.sleep(espera)
                espera = min(espera * 2, 30)

    def _procesar(self, texto) -> None:
        if not isinstance(texto, str) or not texto.startswith(("{", "[")):
            return
        try:
            datos = json.loads(texto)
        except ValueError:
            return
        for mensaje in datos if isinstance(datos, list) else [datos]:
            if not isinstance(mensaje, dict):
                continue
            slug = mensaje.get("slug")
            # Sin "slug" no se puede saber de qué partido es: se guarda por si acaso.
            if slug is None or str(slug).startswith("nba-"):
                self.grabador.marcador(mensaje)
