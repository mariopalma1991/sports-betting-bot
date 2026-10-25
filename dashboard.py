"""Dashboard de NBA en el navegador con los widgets de Polymarket.

Muestra los partidos NBA del día, uno por tarjeta: arriba el widget oficial de
Polymarket con las probabilidades en vivo, debajo el marcador de NBA.com
(marcador_nba.py) y abajo los precios de cada mercado
(compra, venta, spread, liquidez y volumen) que también calcula nba_monitor.py.

En la pestaña "Bot (papel)" corre dos estrategias con dinero ficticio y muestra sus
compras, ventas y saldo en vivo (ver bot_papel.py).

Corre un servidor pequeño en tu computadora (solo tú lo ves) y abre el navegador.
Solo lee datos: no toca tu wallet y no coloca órdenes reales.

Uso:
    python dashboard.py               # datos reales
    python dashboard.py --demo        # datos simulados para ver cómo se ve
    python dashboard.py --puerto 8050 --no-abrir

Para la estrategia del comparador: export ODDS_API_KEY=tu_llave antes de abrirlo.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

import requests

from marcador_nba import Marcador, MarcadorDemo
from bot_papel import PRESUPUESTO, SPREAD_CSV, BotComparador, BotSpread
from nba_monitor import (
    BUSCAR_PARTIDOS_CADA, COMPLETAR_LIBROS_CADA, FuenteDemo, FuenteEnVivo, Mercado,
    analizar, con_libro, mercados_de_evento, obtener_partidos_nba, _fecha,
)

PAGINA = Path(__file__).with_name("dashboard.html")
EMBED_URL = "https://embed.polymarket.com/sports"
EVENTO_URL = "https://polymarket.com/event/"
CREADOR = "Marhum"  # tu usuario de Polymarket: va en cada widget


@dataclass
class Partido:
    slug: str
    titulo: str
    inicio: datetime | None
    mercados: list[Mercado]


def es_margen_exacto(evento: dict) -> bool:
    """Polymarket publica "Pacers vs. Nets - Exact Margin" como un evento aparte."""
    return "exact margin" in (evento.get("title") or "").lower() or "exact-margin" in (evento.get("slug") or "")


def partido_de_evento(evento: dict) -> Partido:
    mercados = mercados_de_evento(evento)
    inicio = next((m.inicio for m in mercados if m.inicio), None) or _fecha(evento.get("startDate"))
    return Partido(evento.get("slug") or "", evento.get("title") or "?", inicio, mercados)


def url_widget(slug: str, tema: str = "dark") -> str:
    return f"{EMBED_URL}?{urlencode({'market': slug, 'theme': tema, 'creator': CREADOR, 'height': 300})}"


def a_json(partido: Partido, libros: dict, demo: bool = False, marcador: dict | None = None) -> dict:
    filas = [analizar(m, libros.get(m.token_id)) for m in partido.mercados]
    return {
        "slug": partido.slug,
        "titulo": partido.titulo,
        "inicio": partido.inicio.isoformat() if partido.inicio else None,
        "widget": None if demo else url_widget(partido.slug),
        "enlace": None if demo else EVENTO_URL + partido.slug,
        "marcador": marcador,
        "mercados": [{
            "tipo": "Ganador" if f.tipo == "moneyline" else f.tipo, "resultado": f.resultado, "compra": f.compra, "venta": f.venta,
            "spread": f.spread, "tick": f.tick, "profundidad": round(f.profundidad, 2),
            "volumen_24h": round(f.volumen_24h, 2), "oportunidad": f.oportunidad,
            # Los mercados sin órdenes se mandan igual; la página los oculta.
            "con_libro": bool(con_libro([f])),
        } for f in filas],
    }


def partidos_demo(fuente: FuenteDemo) -> list[Partido]:
    partidos: dict[str, Partido] = {}
    for m in fuente.mercados:
        if m.partido not in partidos:
            slug = "demo-" + m.partido.lower().replace(" vs. ", "-")
            partidos[m.partido] = Partido(slug, m.partido, m.inicio, [])
        partidos[m.partido].mercados.append(m)
    return list(partidos.values())


class Tablero:
    """Mantiene la lista de partidos y los libros al día en un hilo aparte."""

    def __init__(self, demo: bool, comparador: BotComparador | None = None, presupuesto: float = PRESUPUESTO):
        self.demo = demo
        # En demo el bot no guarda nada: así no ensucia el saldo de los datos reales.
        self.spread = BotSpread(presupuesto, csv_ruta=None if demo else SPREAD_CSV)
        self.comparador = comparador or BotComparador(None, presupuesto, demo=demo)
        self.sesion = requests.Session()
        self.fuente = FuenteDemo() if demo else FuenteEnVivo()
        self.partidos: list[Partido] = partidos_demo(self.fuente) if demo else []
        self.marcador = MarcadorDemo([(p.titulo, p.inicio) for p in self.partidos]) if demo else Marcador()
        self.error: str | None = None
        self.candado = threading.Lock()

    def iniciar(self) -> None:
        if not self.demo:
            self.buscar_partidos()
        self.fuente.iniciar()
        self.marcador.iniciar()
        if not self.demo:
            threading.Thread(target=self._bucle, daemon=True).start()
        threading.Thread(target=self._bucle_spread, daemon=True).start()
        threading.Thread(target=self._bucle_comparador, daemon=True).start()

    def buscar_partidos(self) -> None:
        try:
            partidos = [partido_de_evento(e) for e in obtener_partidos_nba(self.sesion) if not es_margen_exacto(e)]
            with self.candado:
                self.partidos = partidos
                self.error = None
            self.fuente.suscribir([m.token_id for p in partidos for m in p.mercados])
            self.fuente.completar_libros(self.sesion)
        except requests.RequestException as e:
            self.error = str(e)

    def _bucle(self) -> None:
        ultima_busqueda = time.monotonic()
        while True:
            time.sleep(COMPLETAR_LIBROS_CADA)
            if time.monotonic() - ultima_busqueda > BUSCAR_PARTIDOS_CADA:
                ultima_busqueda = time.monotonic()
                self.buscar_partidos()
            else:
                try:
                    self.fuente.completar_libros(self.sesion)
                except requests.RequestException as e:
                    self.error = f"al pedir libros: {e}"

    def _bucle_spread(self) -> None:
        while True:
            time.sleep(1)
            libros = self.fuente.copia()
            with self.candado:
                mercados = [m for p in self.partidos for m in p.mercados]
            filas = {m.token_id: analizar(m, libros.get(m.token_id)) for m in mercados}
            self.spread.paso(filas, self.fuente.sacar_operaciones())

    def _bucle_comparador(self) -> None:
        sesion = requests.Session()
        while True:
            self.comparador.paso(sesion)
            time.sleep(30)

    def foto_bot(self) -> dict:
        return {"demo": self.demo, "hora": datetime.now(timezone.utc).isoformat(),
                "estrategias": [self.spread.foto(), self.comparador.foto()]}

    def foto(self) -> dict:
        libros = self.fuente.copia()
        with self.candado:
            partidos = sorted(self.partidos, key=lambda p: p.inicio or datetime.max.replace(tzinfo=timezone.utc))
        return {
            "demo": self.demo,
            "estado": self.fuente.estado,
            "error": self.error,
            "hora": datetime.now(timezone.utc).isoformat(),
            "partidos": [a_json(p, libros, self.demo, self.marcador.de(p.titulo, p.slug)) for p in partidos],
        }


def manejador(tablero: Tablero) -> type[BaseHTTPRequestHandler]:
    class Manejador(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                self._responder(PAGINA.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/partidos":
                self._responder(json.dumps(tablero.foto()).encode(), "application/json")
            elif self.path == "/api/bot":
                self._responder(json.dumps(tablero.foto_bot()).encode(), "application/json")
            else:
                self.send_error(404)

        def _responder(self, cuerpo: bytes, tipo: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", tipo)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(cuerpo)

        def log_message(self, *args):
            pass  # sin una línea por cada consulta de la página

    return Manejador


def main() -> None:
    parser = argparse.ArgumentParser(description="Dashboard de NBA con los widgets de Polymarket")
    parser.add_argument("--demo", action="store_true", help="usar datos simulados")
    parser.add_argument("--puerto", type=int, default=8050, help="puerto local (por defecto 8050)")
    parser.add_argument("--no-abrir", action="store_true", help="no abrir el navegador solo")
    bot = parser.add_argument_group("bot en papel (sin dinero real)")
    bot.add_argument("--presupuesto", type=float, default=PRESUPUESTO,
                     help=f"dólares ficticios con los que empieza cada estrategia (por defecto {PRESUPUESTO:g})")
    bot.add_argument("--llave", default=os.environ.get("ODDS_API_KEY"),
                     help="llave de The Odds API para el comparador (o variable ODDS_API_KEY)")
    bot.add_argument("--min-ventaja", type=float, default=0.01,
                     help="ventaja mínima del comparador para comprar (por defecto 0.01 = 1¢)")
    bot.add_argument("--momios-cada", type=float, default=1800,
                     help="segundos entre consultas de momios; cada una gasta 1 crédito (por defecto 1800)")
    args = parser.parse_args()

    comparador = BotComparador(args.llave, args.presupuesto, min_ventaja=args.min_ventaja,
                               momios_cada=args.momios_cada, demo=args.demo)
    tablero = Tablero(args.demo, comparador, args.presupuesto)
    print("Buscando partidos NBA en Polymarket…" if not args.demo else "Modo demo: datos simulados.")
    tablero.iniciar()
    # 127.0.0.1: solo tu computadora puede abrir la página.
    servidor = ThreadingHTTPServer(("127.0.0.1", args.puerto), manejador(tablero))
    url = f"http://localhost:{args.puerto}"
    print(f"Dashboard en {url}  (Ctrl+C para salir)")
    if not args.no_abrir:
        webbrowser.open(url)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
