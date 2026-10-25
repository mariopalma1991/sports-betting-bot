"""Marcador en vivo de NBA.com para el dashboard.

Lee el marcador del día que publica NBA.com (gratis, sin llave) y lo resume por
partido: puntos, cuarto, reloj, puntos por cuarto y el líder de cada equipo.
Cada partido de Polymarket ("Lakers vs. Jazz") se une con el suyo por el nombre
de los equipos o, si no, por las siglas del slug ("nba-lal-uta-2026-10-25").
"""

from __future__ import annotations

import random
import re
import threading
import time
from datetime import datetime

import requests

MARCADOR_URL = "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"
CONSULTAR_CADA = 5  # segundos; el archivo de NBA.com está en caché y no cambia más rápido que eso

POR_EMPEZAR, EN_JUEGO, FINAL = 1, 2, 3


def reloj(texto: str | None) -> str:
    """"PT04M12.00S" -> "4:12"; "PT00M38.40S" -> "0:38"."""
    m = re.match(r"PT(\d+)M(\d+)", texto or "")
    return f"{int(m.group(1))}:{m.group(2)}" if m else ""


def nombre_cuarto(periodo: int, regulares: int = 4) -> str:
    return f"Q{periodo}" if periodo <= regulares else f"OT{periodo - regulares}" if periodo > regulares + 1 else "OT"


def _equipo(e: dict) -> dict:
    return {
        "siglas": e.get("teamTricode") or "",
        "nombre": e.get("teamName") or "",
        "puntos": e.get("score"),
        "cuartos": [p.get("score") for p in e.get("periods") or []],
    }


def _lider(l: dict | None) -> dict | None:
    if not l or not l.get("name"):
        return None
    return {"nombre": l["name"], "equipo": l.get("teamTricode") or "",
            "puntos": l.get("points"), "rebotes": l.get("rebounds"), "asistencias": l.get("assists")}


def resumir(juego: dict) -> dict:
    """Un partido del JSON de NBA.com, solo con lo que muestra la tarjeta."""
    estado = juego.get("gameStatus")
    periodo = juego.get("period") or 0
    regulares = juego.get("regulationPeriods") or 4
    if estado == EN_JUEGO:
        tiempo = reloj(juego.get("gameClock"))
        # Entre cuartos y en el medio tiempo el reloj llega vacío o en 0:00; ahí el texto de NBA.com es mejor.
        etiqueta = (f"{nombre_cuarto(periodo, regulares)} · {tiempo}" if tiempo and tiempo != "0:00"
                    else juego.get("gameStatusText") or nombre_cuarto(periodo, regulares))
    elif estado == FINAL:
        etiqueta = "Final" if periodo <= regulares else f"Final ({nombre_cuarto(periodo, regulares)})"
    else:
        etiqueta = ""  # la página muestra la hora de inicio en tu zona horaria
    lideres = juego.get("gameLeaders") or {}
    return {
        "id": juego.get("gameId"),
        "estado": {POR_EMPEZAR: "por empezar", EN_JUEGO: "en juego", FINAL: "final"}.get(estado, "por empezar"),
        "etiqueta": etiqueta,
        "inicio": juego.get("gameTimeUTC"),
        "cuartos": max(regulares, periodo),
        "visitante": _equipo(juego.get("awayTeam") or {}),
        "local": _equipo(juego.get("homeTeam") or {}),
        "lideres": [l for l in (_lider(lideres.get("awayLeaders")), _lider(lideres.get("homeLeaders"))) if l],
    }


def obtener_juegos(sesion: requests.Session) -> list[dict]:
    r = sesion.get(MARCADOR_URL, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return [resumir(j) for j in (r.json().get("scoreboard") or {}).get("games") or []]


def emparejar(titulo: str, slug: str, juegos: list[dict]) -> dict | None:
    """El juego de NBA.com que corresponde a un partido de Polymarket, o None."""
    nombres = {n.strip().lower() for n in titulo.split(" - ")[0].split(" vs. ")}
    partes = slug.split("-")
    siglas = {s.upper() for s in partes[1:3]} if len(partes) >= 3 and partes[0] == "nba" else set()
    for juego in juegos:
        equipos = (juego["visitante"], juego["local"])
        if {e["nombre"].lower() for e in equipos} == nombres or {e["siglas"] for e in equipos} == siglas:
            return juego
    return None


class Marcador:
    """Mantiene el marcador del día al día en un hilo aparte."""

    def __init__(self):
        self.juegos: list[dict] = []
        self.error: str | None = None
        self.candado = threading.Lock()

    def iniciar(self) -> None:
        threading.Thread(target=self._bucle, daemon=True).start()

    def _bucle(self) -> None:
        sesion = requests.Session()
        while True:
            try:
                juegos = obtener_juegos(sesion)
                with self.candado:
                    self.juegos, self.error = juegos, None
            except (requests.RequestException, ValueError) as e:
                self.error = str(e)  # se queda el último marcador que llegó
            time.sleep(CONSULTAR_CADA)

    def de(self, titulo: str, slug: str) -> dict | None:
        with self.candado:
            return emparejar(titulo, slug, self.juegos)


class MarcadorDemo(Marcador):
    """Inventa un marcador que avanza, para --demo."""

    def __init__(self, partidos: list[tuple[str, datetime | None]]):
        super().__init__()
        self._partidos = partidos
        self._inicio = time.time()

    def iniciar(self) -> None:
        self._avanzar()
        threading.Thread(target=self._bucle, daemon=True).start()

    def _bucle(self) -> None:
        while True:
            time.sleep(CONSULTAR_CADA)
            self._avanzar()

    def _avanzar(self) -> None:
        segundos = int(time.time() - self._inicio)
        juegos = []
        for i, (titulo, _inicio) in enumerate(self._partidos):
            visitante, local = [n.strip() for n in titulo.split(" vs. ")]
            # El primero va en juego, el segundo ya terminó y el resto está por empezar.
            estado = EN_JUEGO if i == 0 else FINAL if i == 1 else POR_EMPEZAR
            azar = random.Random(i)
            cuartos_v = [azar.randint(20, 34) for _ in range(4)]
            cuartos_l = [azar.randint(20, 34) for _ in range(4)]
            if estado == EN_JUEGO:
                cuartos_v = cuartos_v[:2] + [cuartos_v[2] * (segundos % 120) // 120]
                cuartos_l = cuartos_l[:2] + [cuartos_l[2] * (segundos % 120) // 120]
            elif estado == POR_EMPEZAR:
                cuartos_v = cuartos_l = []
            restante = 720 - (segundos * 6) % 720
            juegos.append(resumir({
                "gameId": f"demo-{i}", "gameStatus": estado, "period": len(cuartos_v),
                "gameClock": f"PT{restante // 60:02d}M{restante % 60:02d}.00S", "gameTimeUTC": _inicio.isoformat() if _inicio else None,
                "awayTeam": {"teamName": visitante, "teamTricode": visitante[:3].upper(),
                             "score": sum(cuartos_v), "periods": [{"score": s} for s in cuartos_v]},
                "homeTeam": {"teamName": local, "teamTricode": local[:3].upper(),
                             "score": sum(cuartos_l), "periods": [{"score": s} for s in cuartos_l]},
                "gameLeaders": {} if estado == POR_EMPEZAR else {
                    "awayLeaders": {"name": f"Jugador de {visitante}", "teamTricode": visitante[:3].upper(),
                                    "points": sum(cuartos_v) // 4, "rebounds": 7, "assists": 5},
                    "homeLeaders": {"name": f"Jugador de {local}", "teamTricode": local[:3].upper(),
                                    "points": sum(cuartos_l) // 4, "rebounds": 9, "assists": 3},
                },
            }))
        with self.candado:
            self.juegos = juegos
