"""Marcador en vivo de Polymarket para el dashboard.

Escucha el canal de deportes de Polymarket (el mismo que graba grabador.py) y
guarda el último marcador y reloj de cada partido. Llega al instante por WebSocket.

El canal casi nunca trae el slug del evento ("nba-lal-uta-2026-10-25"): trae gameId,
leagueAbbreviation, homeTeam y awayTeam ("Los Angeles Lakers"). Por eso cada tarjeta
busca su partido por los equipos de su título ("Lakers vs. Jazz").
"""

from __future__ import annotations

import json
import threading
import time

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from grabador import SPORTS_WS_URL

# Se considera pausa (tiempo fuera, tiros libres, entre cuartos) cuando el reloj no ha
# cambiado en PAUSA_RELOJ segundos y nadie ha anotado en PAUSA_PUNTOS segundos.
PAUSA_RELOJ = 5
PAUSA_PUNTOS = 3


def clave(slug: str) -> str:
    """El mismo partido con o sin "nba-" adelante ("nba-lal-uta-…" y "lal-uta-…")."""
    return slug[4:] if slug.startswith("nba-") else slug


def equipos_de(slug: str, titulo: str) -> list[tuple[str, str]]:
    """("nba-lal-uta-…", "Lakers vs. Jazz") -> [("lakers", "lal"), ("jazz", "uta")]."""
    partes = slug.split("-")
    nombres = [t.strip().lower() for t in titulo.replace(" vs ", " vs. ").split(" vs. ")]
    if len(partes) < 3 or len(nombres) != 2 or not all(nombres):
        return []
    return list(zip(nombres, partes[1:3]))


def es_equipo(equipo: tuple[str, str], texto) -> bool:
    """El canal manda el nombre completo ("Los Angeles Lakers") o las siglas ("LAL")."""
    texto = str(texto or "").lower().strip()
    return bool(texto) and (texto == equipo[1] or equipo[0] in texto)


def es_de(m: dict, equipos: list[tuple[str, str]]) -> bool:
    """El mensaje es del partido NBA con esos dos equipos."""
    if str(m.get("leagueAbbreviation") or "nba").lower() != "nba" or not equipos:
        return False
    return all(any(es_equipo(e, m.get(lado)) for lado in ("homeTeam", "awayTeam")) for e in equipos)


def resumir(slug: str, m: dict, pausa: bool = False, equipos: list[tuple[str, str]] | None = None) -> dict:
    """Lo que muestra la tarjeta: puntos, cuarto, reloj y si está en vivo o terminó.

    Los puntos van en el orden de las siglas del slug ("nba-lal-uta" -> LAL, UTA).
    El canal manda "local-visitante"; si se sabe quién es local, se acomodan.
    """
    partes = slug.split("-")
    siglas = [s.upper() for s in partes[1:3]] if len(partes) >= 3 else ["", ""]
    puntos = [p.strip() for p in str(m.get("score") or "").split("-")]
    if len(puntos) != 2 or not all(p.isdigit() for p in puntos):
        puntos = [None, None]
    elif equipos and m.get("homeTeam") and not es_equipo(equipos[0], m["homeTeam"]):
        puntos.reverse()  # el primero del título es el visitante
    terminado = bool(m.get("ended")) or str(m.get("status") or "").lower() in ("final", "ended", "finished")
    return {
        "equipos": [{"siglas": s, "puntos": int(p) if p is not None else None} for s, p in zip(siglas, puntos)],
        "cuarto": m.get("period") or "",
        "reloj": m.get("elapsed") or "",
        "estado": "final" if terminado else "en juego" if m.get("live") else "por empezar",
        "pausa": pausa,
    }


class Marcador:
    """Último mensaje de cada partido NBA, actualizado por WebSocket en un hilo aparte."""

    def __init__(self, url: str = SPORTS_WS_URL):
        self.url = url
        # El canal manda todos los deportes; se guardan todos y la tarjeta busca el suyo.
        self.partidos: dict[str, dict] = {}  # clave(slug) o "id-<gameId>" -> campos recibidos (se van completando)
        self.titulos: dict[str, str] = {}  # slug del evento -> "Lakers vs. Jazz", para buscar por equipos
        self.cambios: dict[str, dict[str, float]] = {}  # slug -> cuándo cambió por última vez el reloj y el marcador
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

    def procesar(self, texto, ahora: float | None = None) -> None:
        if not isinstance(texto, str) or not texto.startswith(("{", "[")):
            return
        try:
            datos = json.loads(texto)
        except ValueError:
            return
        ahora = time.time() if ahora is None else ahora
        with self.candado:
            for m in datos if isinstance(datos, list) else [datos]:
                if not isinstance(m, dict):
                    continue
                slug = clave(str(m["slug"])) if m.get("slug") else f"id-{m['gameId']}" if m.get("gameId") else None
                if slug:
                    # Algunos mensajes traen solo lo que cambió: se combinan con lo anterior.
                    antes = self.partidos.get(slug, {})
                    cambios = self.cambios.setdefault(slug, {"reloj": ahora, "puntos": ahora})
                    for campo, cual in (("elapsed", "reloj"), ("score", "puntos")):
                        if campo in m and m[campo] != antes.get(campo):
                            cambios[cual] = ahora
                    self.partidos[slug] = {**antes, **m}

    def _buscar(self, slug: str) -> str | None:
        """Dónde está guardado el partido de esa tarjeta: por slug o por sus equipos."""
        if clave(slug) in self.partidos:
            return clave(slug)
        equipos = equipos_de(slug, self.titulos.get(slug, ""))
        iguales = [k for k, m in self.partidos.items() if es_de(m, equipos)]
        # Si los mismos equipos jugaron ayer, gana el que está en juego (o el más reciente).
        return next((k for k in iguales if self.partidos[k].get("live")), iguales[-1] if iguales else None)

    def en_pausa(self, slug: str, ahora: float | None = None) -> bool:
        """El partido está en juego pero con el reloj detenido y sin canastas recientes."""
        ahora = time.time() if ahora is None else ahora
        with self.candado:
            k = self._buscar(slug)
            m, cambios = self.partidos.get(k), self.cambios.get(k)
        if not m or not cambios or resumir(slug, m)["estado"] != "en juego":
            return False
        return ahora - cambios["reloj"] >= PAUSA_RELOJ and ahora - cambios["puntos"] >= PAUSA_PUNTOS

    def de(self, slug: str) -> dict | None:
        with self.candado:
            m = self.partidos.get(self._buscar(slug))
        return resumir(slug, m, self.en_pausa(slug), equipos_de(slug, self.titulos.get(slug, ""))) if m else None


class MarcadorDemo(Marcador):
    """Inventa un marcador para --demo: el segundo partido terminó y los demás van en juego.

    En cada partido en juego el reloj corre 25 s y se detiene 20 s (una "pausa"), desfasados.
    """

    def __init__(self, slugs: list[str]):
        super().__init__()
        self._slugs = slugs
        self._inicio = time.time()
        self.estado = "demo"

    def iniciar(self) -> None:
        threading.Thread(target=self._simular, daemon=True).start()

    def _simular(self) -> None:
        while True:
            s = int(time.time() - self._inicio)
            mensajes = []
            for i, slug in enumerate(self._slugs):
                if i == 1:
                    mensajes.append({"slug": slug, "live": False, "ended": True, "period": "Q4",
                                     "score": "112-104", "elapsed": "00:00"})
                    continue
                t = s + i * 15
                corrido = (t // 45) * 25 + min(t % 45, 25)  # segundos de reloj que han corrido
                restante = 720 - (corrido * 6) % 720
                mensajes.append({"slug": slug, "live": True, "period": "Q3",
                                 "score": f"{60 + corrido // 8}-{55 + corrido // 10}",
                                 "elapsed": f"{restante // 60:02d}:{restante % 60:02d}"})
            self.procesar(json.dumps(mensajes))
            time.sleep(1)
