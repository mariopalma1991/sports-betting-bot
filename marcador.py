"""Marcador en vivo de Polymarket para el dashboard.

Escucha el canal de deportes de Polymarket (el mismo que graba grabador.py) y
guarda el último marcador y reloj de cada partido NBA, por su slug
("nba-lal-uta-2026-10-25"). Llega al instante por WebSocket.
"""

from __future__ import annotations

import json
import threading
import time

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from grabador import SPORTS_WS_URL


def resumir(slug: str, m: dict) -> dict:
    """Lo que muestra la tarjeta: puntos, cuarto, reloj y si está en vivo o terminó.

    Los puntos van en el orden de las siglas del slug ("nba-lal-uta" -> LAL, UTA).
    """
    partes = slug.split("-")
    siglas = [s.upper() for s in partes[1:3]] if len(partes) >= 3 else ["", ""]
    puntos = [p.strip() for p in str(m.get("score") or "").split("-")]
    if len(puntos) != 2 or not all(p.isdigit() for p in puntos):
        puntos = [None, None]
    terminado = bool(m.get("ended")) or str(m.get("status") or "").lower() in ("final", "ended", "finished")
    return {
        "equipos": [{"siglas": s, "puntos": int(p) if p is not None else None} for s, p in zip(siglas, puntos)],
        "cuarto": m.get("period") or "",
        "reloj": m.get("elapsed") or "",
        "estado": "final" if terminado else "en juego" if m.get("live") else "por empezar",
    }


class Marcador:
    """Último mensaje de cada partido NBA, actualizado por WebSocket en un hilo aparte."""

    PREFIJO = "nba-"  # el canal manda todos los deportes

    def __init__(self, url: str = SPORTS_WS_URL):
        self.url = url
        self.partidos: dict[str, dict] = {}  # slug -> campos recibidos (se van completando)
        self.estado = "conectando…"
        self.candado = threading.Lock()

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
                            self.procesar(texto)
            except (OSError, WebSocketException) as e:
                self.estado = f"reconectando ({e.__class__.__name__})…"
                time.sleep(espera)
                espera = min(espera * 2, 30)

    def procesar(self, texto) -> None:
        if not isinstance(texto, str) or not texto.startswith(("{", "[")):
            return
        try:
            datos = json.loads(texto)
        except ValueError:
            return
        with self.candado:
            for m in datos if isinstance(datos, list) else [datos]:
                slug = m.get("slug") if isinstance(m, dict) else None
                if slug and str(slug).startswith(self.PREFIJO):
                    # Algunos mensajes traen solo lo que cambió: se combinan con lo anterior.
                    self.partidos[slug] = {**self.partidos.get(slug, {}), **m}

    def de(self, slug: str) -> dict | None:
        with self.candado:
            m = self.partidos.get(slug)
        return resumir(slug, m) if m else None


class MarcadorDemo(Marcador):
    """Inventa un marcador que avanza, para --demo: el primer partido va en juego y el segundo terminó."""

    def __init__(self, slugs: list[str]):
        super().__init__()
        self._slugs = slugs
        self._inicio = time.time()
        self.estado = "demo"
        self.PREFIJO = ""  # los partidos de --demo no tienen slug de la NBA

    def iniciar(self) -> None:
        threading.Thread(target=self._simular, daemon=True).start()

    def _simular(self) -> None:
        while True:
            s = int(time.time() - self._inicio)
            restante = 720 - (s * 6) % 720
            mensajes = []
            if self._slugs:
                mensajes.append({"slug": self._slugs[0], "live": True, "period": "Q3",
                                 "score": f"{60 + s // 8}-{55 + s // 10}",
                                 "elapsed": f"{restante // 60:02d}:{restante % 60:02d}"})
            if len(self._slugs) > 1:
                mensajes.append({"slug": self._slugs[1], "live": False, "ended": True, "period": "Q4",
                                 "score": "112-104", "elapsed": "00:00"})
            self.procesar(json.dumps(mensajes))
            time.sleep(1)
