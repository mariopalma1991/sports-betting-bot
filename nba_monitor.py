"""Monitor en tiempo real de spreads y liquidez para partidos NBA en Polymarket.

Busca los partidos NBA abiertos (API Gamma) y se suscribe al WebSocket de
Polymarket, que manda cada cambio del libro de órdenes al instante.

Solo lee datos públicos: no usa llaves, no toca tu wallet y no coloca órdenes.

Uso:
    python nba_monitor.py              # datos reales en tiempo real
    python nba_monitor.py --demo       # datos simulados para ver cómo se ve
    python nba_monitor.py --csv datos.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from grabador import FuenteMarcador, Grabador
from papel import ESTRATEGIAS, SimuladorPapel, panel

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
NBA_TAG_ID = 745
# Slug de un partido, p. ej. "nba-lal-bos-2026-10-24". Sirve para dejar fuera
# los mercados de temporada (campeón, MVP...) que comparten la etiqueta NBA.
GAME_SLUG = re.compile(r"^nba-[a-z]+-[a-z]+-\d{4}-\d{2}-\d{2}")
BUSCAR_PARTIDOS_CADA = 300  # segundos; la lista de partidos cambia poco
COMPLETAR_LIBROS_CADA = 30  # segundos entre intentos de pedir por REST los libros que falten

# Criterios para marcar un mercado como oportunidad de scalping como maker.
# Con 2 ticks libres puedes comprar 1 tick arriba de la mejor compra y vender 1 tick
# abajo de la mejor venta y aún te queda al menos 1 tick de margen.
MIN_TICKS_LIBRES = 2
MAX_SPREAD = 0.06         # más de 6¢ suele indicar un mercado muerto
MIN_VOLUMEN_24H = 5_000   # USD
MIN_PROFUNDIDAD = 200     # USD en el libro a ±2¢ del precio medio
PROFUNDIDAD_RANGO = 0.02
PROFUNDIDAD_BAJA = 100    # por debajo de esto se pinta en rojo


@dataclass
class Mercado:
    partido: str
    tipo: str
    resultado: str
    token_id: str
    inicio: datetime | None
    volumen_24h: float


@dataclass
class Libro:
    """Libro de órdenes de un token: precio -> cantidad de acciones."""
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    tick: float = 0.01
    actualizado: float = 0.0  # time.time() del último cambio

    def reemplazar(self, bids, asks) -> None:
        self.bids = {float(n["price"]): float(n["size"]) for n in bids or []}
        self.asks = {float(n["price"]): float(n["size"]) for n in asks or []}
        self.actualizado = time.time()

    def cambiar(self, lado: str, precio: float, cantidad: float) -> None:
        niveles = self.bids if lado.upper() == "BUY" else self.asks
        if cantidad <= 0:
            niveles.pop(precio, None)
        else:
            niveles[precio] = cantidad
        self.actualizado = time.time()


@dataclass
class Fila:
    token_id: str
    partido: str
    tipo: str
    resultado: str
    inicio: datetime | None
    compra: float | None
    venta: float | None
    spread: float | None
    ticks_libres: int | None
    compra_sugerida: float | None
    venta_sugerida: float | None
    margen: float | None
    tick: float
    profundidad: float
    volumen_24h: float
    oportunidad: bool
    actualizado: float


# --- Partidos (API Gamma) ---------------------------------------------------

def _json_list(valor) -> list:
    """Gamma devuelve algunas listas como texto JSON ("[\"Yes\", \"No\"]")."""
    if isinstance(valor, list):
        return valor
    if not valor:
        return []
    try:
        return json.loads(valor)
    except (TypeError, ValueError):
        return []


def _fecha(valor) -> datetime | None:
    if not valor:
        return None
    try:
        fecha = datetime.fromisoformat(str(valor).replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)


def es_partido(evento: dict) -> bool:
    if GAME_SLUG.match(evento.get("slug") or ""):
        return True
    return any(m.get("gameStartTime") for m in evento.get("markets") or [])


def mercados_de_evento(evento: dict) -> list[Mercado]:
    mercados = []
    for m in evento.get("markets") or []:
        if m.get("closed") or not m.get("enableOrderBook", True):
            continue
        tokens = _json_list(m.get("clobTokenIds"))
        resultados = _json_list(m.get("outcomes"))
        if not tokens:
            continue
        # Basta con el primer resultado: el libro del otro es el espejo (1 - p),
        # así que el spread y la profundidad son los mismos.
        mercados.append(Mercado(
            partido=evento.get("title") or m.get("question") or "?",
            tipo=m.get("groupItemTitle") or m.get("sportsMarketType") or m.get("question") or "",
            resultado=str(resultados[0]) if resultados else "",
            token_id=str(tokens[0]),
            inicio=_fecha(m.get("gameStartTime") or evento.get("startDate")),
            volumen_24h=float(m.get("volume24hr") or 0),
        ))
    return mercados


def obtener_partidos_nba(sesion: requests.Session) -> list[dict]:
    """Eventos de Gamma de los partidos NBA abiertos (sin los mercados de temporada)."""
    partidos: list[dict] = []
    offset = 0
    while True:
        r = sesion.get(f"{GAMMA_URL}/events", params={
            "tag_id": NBA_TAG_ID, "active": "true", "closed": "false",
            "limit": 100, "offset": offset,
        }, timeout=15)
        r.raise_for_status()
        eventos = r.json()
        partidos.extend(e for e in eventos if es_partido(e))
        if len(eventos) < 100:
            return partidos
        offset += 100


def obtener_mercados_nba(sesion: requests.Session) -> list[Mercado]:
    return [m for evento in obtener_partidos_nba(sesion) for m in mercados_de_evento(evento)]


# --- Libros en tiempo real (WebSocket) ---------------------------------------

def aplicar_mensaje(libros: dict[str, Libro], mensaje: dict) -> None:
    """Aplica un mensaje del canal "market" del WebSocket a los libros."""
    tipo = mensaje.get("event_type")
    if tipo == "book":
        libro = libros.setdefault(str(mensaje.get("asset_id")), Libro())
        # Versiones anteriores de la API usaban "buys"/"sells".
        libro.reemplazar(mensaje.get("bids", mensaje.get("buys")),
                         mensaje.get("asks", mensaje.get("sells")))
    elif tipo == "price_change":
        # Formato actual: lista "price_changes", cada uno con su asset_id.
        # Formato anterior: un asset_id arriba y una lista "changes".
        cambios = mensaje.get("price_changes")
        if cambios is None:
            cambios = [{**c, "asset_id": mensaje.get("asset_id")} for c in mensaje.get("changes") or []]
        for c in cambios:
            libro = libros.get(str(c.get("asset_id")))
            if libro is not None:
                libro.cambiar(c["side"], float(c["price"]), float(c["size"]))
    elif tipo == "tick_size_change":
        libro = libros.get(str(mensaje.get("asset_id")))
        if libro is not None:
            libro.tick = float(mensaje.get("new_tick_size") or libro.tick)


class FuenteEnVivo:
    """Mantiene los libros al día escuchando el WebSocket en un hilo aparte."""

    def __init__(self):
        self.libros: dict[str, Libro] = {}
        self.candado = threading.Lock()
        self.tokens: list[str] = []
        self.estado = "conectando…"
        self.operaciones: list[dict] = []  # mensajes last_trade_price aún no leídos
        self.grabador: Grabador | None = None  # si existe, recibe cada mensaje tal como llega
        self._reconectar = threading.Event()

    def suscribir(self, tokens: list[str]) -> None:
        if sorted(tokens) != sorted(self.tokens):
            self.tokens = list(tokens)
            self._reconectar.set()

    def iniciar(self) -> None:
        threading.Thread(target=self._bucle, daemon=True).start()

    def _bucle(self) -> None:
        espera = 1
        while True:
            if not self.tokens:
                time.sleep(1)
                continue
            try:
                with connect(WS_URL, open_timeout=15, ping_interval=None) as ws:
                    ws.send(json.dumps({"type": "market", "assets_ids": self.tokens}))
                    self._reconectar.clear()
                    self.estado = "en vivo"
                    espera = 1
                    ultimo_ping = time.monotonic()
                    while not self._reconectar.is_set():
                        # Polymarket pide un "PING" de texto cada ~10 s para no cortar.
                        if time.monotonic() - ultimo_ping > 10:
                            ws.send("PING")
                            ultimo_ping = time.monotonic()
                        try:
                            texto = ws.recv(timeout=1)
                        except TimeoutError:
                            continue
                        self._procesar(texto)
            except (OSError, WebSocketException) as e:
                self.estado = f"reconectando ({e.__class__.__name__})…"
                time.sleep(espera)
                espera = min(espera * 2, 30)

    def _procesar(self, texto) -> None:
        if not isinstance(texto, str) or not texto.startswith(("{", "[")):
            return  # "PONG" y similares
        datos = json.loads(texto)
        mensajes = datos if isinstance(datos, list) else [datos]
        if self.grabador:
            for mensaje in mensajes:
                self.grabador.libro(mensaje)
        with self.candado:
            for mensaje in mensajes:
                if mensaje.get("event_type") == "last_trade_price":
                    self.operaciones.append(mensaje)
                else:
                    aplicar_mensaje(self.libros, mensaje)

    def completar_libros(self, sesion: requests.Session) -> None:
        """Pide por REST la foto de los libros que el WebSocket todavía no mandó.

        Al suscribirse a muchos mercados, el WebSocket no siempre manda la foto
        inicial de todos; sin ella, los cambios de precio de ese mercado se pierden.
        """
        with self.candado:
            faltan = [t for t in self.tokens if t not in self.libros]
        for i in range(0, len(faltan), 50):
            bloque = faltan[i:i + 50]
            r = sesion.post(f"{CLOB_URL}/books", json=[{"token_id": t} for t in bloque], timeout=15)
            if r.ok:
                libros = r.json()
            else:
                # Si un token del bloque no tiene libro, la API rechaza todo el bloque:
                # se piden de uno en uno y se saltan los que fallen.
                libros = []
                for t in bloque:
                    uno = sesion.get(f"{CLOB_URL}/book", params={"token_id": t}, timeout=15)
                    if uno.ok:
                        libros.append(uno.json())
            with self.candado:
                for libro in libros:
                    token = str(libro.get("asset_id"))
                    if token in self.libros:
                        continue  # el WebSocket llegó primero y es más reciente
                    nuevo = Libro(tick=float(libro.get("tick_size") or 0.01))
                    nuevo.reemplazar(libro.get("bids"), libro.get("asks"))
                    self.libros[token] = nuevo
                    if self.grabador:
                        self.grabador.libro({**libro, "event_type": "book", "origen": "rest"})

    def sacar_operaciones(self) -> list[dict]:
        with self.candado:
            operaciones, self.operaciones = self.operaciones, []
            return operaciones

    def copia(self) -> dict[str, Libro]:
        with self.candado:
            return {t: Libro(dict(l.bids), dict(l.asks), l.tick, l.actualizado) for t, l in self.libros.items()}


# --- Cálculos ---------------------------------------------------------------

def analizar(mercado: Mercado, libro: Libro | None) -> Fila:
    libro = libro or Libro()
    compra = max((p for p, s in libro.bids.items() if s > 0), default=None)
    venta = min((p for p, s in libro.asks.items() if s > 0), default=None)

    spread = ticks_libres = compra_sugerida = venta_sugerida = margen = None
    profundidad = 0.0
    if compra is not None and venta is not None:
        spread = round(venta - compra, 4)
        ticks_libres = max(round(spread / libro.tick) - 1, 0)
        # Precios sugeridos como maker: mejorar en 1 tick la mejor compra y la mejor
        # venta para quedar primero en la fila de cada lado. Solo tiene sentido si
        # entre los dos precios queda margen.
        if ticks_libres >= MIN_TICKS_LIBRES:
            compra_sugerida = round(compra + libro.tick, 4)
            venta_sugerida = round(venta - libro.tick, 4)
            margen = round(venta_sugerida - compra_sugerida, 4)
        medio = (compra + venta) / 2
        niveles = list(libro.bids.items()) + list(libro.asks.items())
        profundidad = sum(p * s for p, s in niveles if abs(p - medio) <= PROFUNDIDAD_RANGO + 1e-9)

    oportunidad = (
        spread is not None
        and ticks_libres >= MIN_TICKS_LIBRES
        and spread <= MAX_SPREAD
        and mercado.volumen_24h >= MIN_VOLUMEN_24H
        and profundidad >= MIN_PROFUNDIDAD
    )
    return Fila(
        token_id=mercado.token_id, partido=mercado.partido, tipo=mercado.tipo, resultado=mercado.resultado,
        inicio=mercado.inicio, compra=compra, venta=venta, spread=spread,
        ticks_libres=ticks_libres, compra_sugerida=compra_sugerida,
        venta_sugerida=venta_sugerida, margen=margen, tick=libro.tick,
        profundidad=profundidad,
        volumen_24h=mercado.volumen_24h, oportunidad=oportunidad,
        actualizado=libro.actualizado,
    )


def ordenar(filas: list[Fila]) -> list[Fila]:
    """Oportunidades primero; luego los spreads razonables con más espacio y volumen.

    Los mercados sin libro o con spreads de más de MAX_SPREAD (casi siempre sin
    actividad) van al final, aunque su spread sea enorme.
    """
    def clave(f: Fila):
        razonable = f.spread is not None and f.spread <= MAX_SPREAD + 1e-9
        return (not f.oportunidad, not razonable, -(f.ticks_libres or 0) if razonable else 0, -f.volumen_24h)
    return sorted(filas, key=clave)


def con_libro(filas: list[Fila]) -> list[Fila]:
    return [f for f in filas if f.compra is not None or f.venta is not None]


# --- Datos simulados (--demo) -------------------------------------------------

class FuenteDemo(FuenteEnVivo):
    """Igual que la fuente en vivo, pero inventa cambios del libro varias veces por segundo."""

    PARTIDOS = [
        ("Lakers vs. Celtics", "Lakers"), ("Heat vs. Knicks", "Heat"),
        ("Warriors vs. Suns", "Warriors"), ("Bucks vs. 76ers", "Bucks"),
        ("Nuggets vs. Mavericks", "Nuggets"),
    ]

    def __init__(self):
        super().__init__()
        ahora = datetime.now(timezone.utc)
        self.mercados: list[Mercado] = []
        self._medio: dict[str, float] = {}
        self._liquidez: dict[str, float] = {}
        for i, (partido, local) in enumerate(self.PARTIDOS):
            inicio = ahora + timedelta(hours=1 + 1.5 * i)
            for tipo, resultado in (("Ganador", local), ("Hándicap -4.5", local), ("O/U 224.5", "Over")):
                token = f"demo-{len(self.mercados)}"
                self.mercados.append(Mercado(partido, tipo, resultado, token, inicio,
                                             random.choice([800, 4_000, 12_000, 40_000, 85_000])))
                self._medio[token] = random.uniform(0.3, 0.7)
                self._liquidez[token] = random.choice([60, 250, 900, 3_000])
                self._nuevo_libro(token, random.choice([0.01, 0.02, 0.03, 0.05]))
        self.estado = "demo"

    def _nuevo_libro(self, token: str, spread: float) -> None:
        medio, liquidez = self._medio[token], self._liquidez[token]
        compra = round(medio - spread / 2, 2)
        venta = round(compra + spread, 2)
        libro = self.libros.setdefault(token, Libro())
        libro.reemplazar(
            [{"price": round(compra - k * 0.01, 2), "size": round(liquidez / medio / (k + 1))} for k in range(4)],
            [{"price": round(venta + k * 0.01, 2), "size": round(liquidez / medio / (k + 1))} for k in range(4)],
        )

    def iniciar(self) -> None:
        threading.Thread(target=self._simular, daemon=True).start()

    def _simular(self) -> None:
        while True:
            time.sleep(random.uniform(0.05, 0.3))
            token = random.choice(self.mercados).token_id
            with self.candado:
                if random.random() < 0.4:
                    # Una operación inventada contra el mejor precio de un lado.
                    libro = self.libros[token]
                    lado = random.choice(["BUY", "SELL"])
                    precio = min(libro.asks) if lado == "BUY" else max(libro.bids)
                    self.operaciones.append({"event_type": "last_trade_price", "asset_id": token,
                                             "price": str(precio), "side": lado, "size": "10"})
                self._medio[token] = min(max(self._medio[token] + random.gauss(0, 0.01), 0.05), 0.95)
                self._nuevo_libro(token, random.choice([0.01, 0.02, 0.03, 0.04]))


# --- Pantalla ----------------------------------------------------------------

def _dinero(valor: float) -> str:
    if valor >= 1_000:
        return f"${valor / 1_000:,.1f}k"
    return f"${valor:,.0f}"


def _precio(valor: float | None, tick: float = 0.01) -> str:
    if valor is None:
        return "—"
    # Cerca de 0 o 1 Polymarket usa ticks de 0.001.
    return f"{valor:.3f}" if tick < 0.01 else f"{valor:.2f}"


def _centavos(valor: float | None) -> str:
    return "—" if valor is None else f"{valor * 100:.1f}¢".replace(".0¢", "¢")


def _hace(segundos: float) -> str:
    if segundos < 60:
        return f"{segundos:.0f}s"
    if segundos < 3600:
        return f"{segundos / 60:.0f}m"
    return f"{segundos / 3600:.0f}h"


def tabla(filas: list[Fila], estado: str, demo: bool, error: str | None = None) -> Group:
    titulo = "NBA · Polymarket" + ("  [DEMO: datos simulados]" if demo else "")
    ahora = time.time()
    t = Table(title=titulo, caption=f"{estado} · {datetime.now():%H:%M:%S}", header_style="bold", expand=True)
    t.add_column("", width=2)
    t.add_column("Partido")
    t.add_column("Mercado")
    t.add_column("Lado")
    t.add_column("Inicio", justify="right")
    t.add_column("Compra", justify="right")
    t.add_column("Venta", justify="right")
    t.add_column("Spread", justify="right")
    t.add_column("Compra sug.", justify="right", style="cyan")
    t.add_column("Venta sug.", justify="right", style="cyan")
    t.add_column("Margen", justify="right", style="cyan")
    t.add_column("Libro ±2¢", justify="right")
    t.add_column("Vol 24h", justify="right")
    t.add_column("Cambio", justify="right")

    for f in filas:
        spread = _centavos(f.spread)
        if f.oportunidad:
            spread_estilo = "bold green"
        elif f.ticks_libres == 0:
            spread_estilo = "dim"
        else:
            spread_estilo = ""
        edad = ahora - f.actualizado if f.actualizado else None
        t.add_row(
            Text("●", style="green") if f.oportunidad else "",
            f.partido, f.tipo, f.resultado,
            f"{f.inicio.astimezone():%d/%m %H:%M}" if f.inicio else "—",
            _precio(f.compra, f.tick), _precio(f.venta, f.tick),
            Text(spread, style=spread_estilo),
            _precio(f.compra_sugerida, f.tick), _precio(f.venta_sugerida, f.tick), _centavos(f.margen),
            Text(_dinero(f.profundidad), style="red" if f.profundidad < PROFUNDIDAD_BAJA else ""),
            _dinero(f.volumen_24h),
            # Amarillo cuando el libro cambió hace menos de 2 s.
            Text("—" if edad is None else _hace(edad), style="yellow" if edad is not None and edad < 2 else "dim"),
        )

    leyenda = Text.assemble(
        ("● ", "green"), f"oportunidad: al menos {MIN_TICKS_LIBRES} ticks libres entre compra y venta, ",
        f"spread ≤ {MAX_SPREAD * 100:.0f}¢, volumen ≥ {_dinero(MIN_VOLUMEN_24H)}, libro ≥ {_dinero(MIN_PROFUNDIDAD)}.  ",
        ("rojo", "red"), " = poca liquidez.  ", ("gris", "dim"), " = spread de 1 tick, sin espacio.  ",
        ("Cambio", "yellow"), " = hace cuánto se movió el libro.\n",
        ("Compra/Venta sug.", "cyan"), " = 1 tick mejor que la mejor compra y la mejor venta; ",
        ("Margen", "cyan"), " = lo que ganas por acción si se llenan las dos. No es consejo: el precio puede moverse.",
    )
    partes = [t, leyenda]
    if error:
        partes.append(Text(f"Error: {error}", style="yellow"))
    if not filas:
        partes.append(Text("No hay partidos NBA abiertos ahora mismo.", style="yellow"))
    return Group(*partes)


def guardar_csv(ruta: Path, filas: list[Fila]) -> None:
    nueva = not ruta.exists()
    momento = datetime.now(timezone.utc).isoformat()
    with ruta.open("a", newline="", encoding="utf-8") as archivo:
        escritor = csv.DictWriter(archivo, fieldnames=["momento"] + list(Fila.__dataclass_fields__))
        if nueva:
            escritor.writeheader()
        for f in filas:
            fila = asdict(f)
            fila["inicio"] = f.inicio.isoformat() if f.inicio else ""
            escritor.writerow({"momento": momento, **fila})


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor en tiempo real de spreads NBA en Polymarket")
    parser.add_argument("--demo", action="store_true", help="usar datos simulados")
    parser.add_argument("--csv", type=Path, help="guardar una foto de la tabla en este CSV")
    parser.add_argument("--csv-cada", type=float, default=10, help="segundos entre fotos del CSV (por defecto 10)")
    parser.add_argument("--grabar", type=Path, metavar="CARPETA",
                        help="grabar en esta carpeta cada mensaje del libro, el marcador y la hora de inicio de los partidos")
    parser.add_argument("--una-vez", action="store_true", help="mostrar la tabla una vez y salir")
    parser.add_argument("--max-filas", type=int, help="mostrar solo las primeras N filas de la tabla")
    parser.add_argument("--mostrar-vacios", action="store_true", help="mostrar también los mercados sin órdenes")
    papel_args = parser.add_argument_group("simulación en papel (sin dinero real)")
    papel_args.add_argument("--papel", action="store_true",
                            help="simular las estrategias (A, B, C...) a la vez con órdenes ficticias")
    papel_args.add_argument("--estrategias", default=",".join(ESTRATEGIAS),
                            help=f"cuáles simular, separadas por coma (por defecto todas: {','.join(ESTRATEGIAS)})")
    papel_args.add_argument("--presupuesto", type=float, default=7.0,
                            help="dólares disponibles para cada estrategia (por defecto 7)")
    papel_args.add_argument("--acciones", type=float, default=5, help="acciones por orden (por defecto 5, el mínimo usual)")
    papel_args.add_argument("--stop", type=float, default=0.03, help="vender si el precio baja esto desde tu compra (por defecto 0.03)")
    papel_args.add_argument("--espera", type=float, default=60, help="segundos antes de cancelar una compra sin llenar (por defecto 60)")
    papel_args.add_argument("--comision-taker", type=float, default=0.0,
                            help="comisión del stop como fracción, p. ej. 0.01 = 1%% (por defecto 0)")
    papel_args.add_argument("--papel-csv", type=Path, default=Path("papel_estrategias.csv"),
                            help="dónde guardar las operaciones cerradas (por defecto papel_estrategias.csv)")
    args = parser.parse_args()
    if args.grabar and args.demo:
        parser.error("--grabar no se puede usar con --demo: los datos simulados no sirven para analizar")
    if args.papel and args.max_filas is None:
        args.max_filas = 10  # deja espacio en pantalla para el panel de la simulación

    consola = Console()
    sesion = requests.Session()
    fuente = FuenteDemo() if args.demo else FuenteEnVivo()
    grabador = Grabador(args.grabar) if args.grabar else None
    marcador = FuenteMarcador(grabador) if grabador else None
    fuente.grabador = grabador
    mercados: list[Mercado] = fuente.mercados if args.demo else []
    error = None
    ultima_busqueda = ultimo_csv = ultimo_completar = 0.0
    nombres = [n.strip().upper() for n in args.estrategias.split(",") if n.strip()]
    if args.papel and (desconocidas := [n for n in nombres if n not in ESTRATEGIAS]):
        parser.error(f"estrategia desconocida: {', '.join(desconocidas)}")
    simuladores = [SimuladorPapel(n, ESTRATEGIAS[n], args.presupuesto, args.acciones, args.stop, args.espera,
                                  args.comision_taker, args.papel_csv) for n in nombres] if args.papel else []

    def buscar_partidos() -> None:
        nonlocal mercados, error, ultima_busqueda
        ultima_busqueda = time.monotonic()
        try:
            mercados = obtener_mercados_nba(sesion)
            if grabador:
                grabador.partidos([{**asdict(m), "inicio": m.inicio.isoformat() if m.inicio else None}
                                   for m in mercados])
            fuente.suscribir([m.token_id for m in mercados])
            error = None
        except requests.RequestException as e:
            error = str(e)

    def completar_libros() -> None:
        nonlocal error, ultimo_completar
        ultimo_completar = time.monotonic()
        try:
            fuente.completar_libros(sesion)
        except requests.RequestException as e:
            error = f"al pedir libros: {e}"

    def pantalla() -> tuple[Group, list[Fila]]:
        libros = fuente.copia()
        filas = ordenar([analizar(m, libros.get(m.token_id)) for m in mercados])
        operaciones = fuente.sacar_operaciones()
        visibles = filas if args.mostrar_vacios else con_libro(filas)
        estado = fuente.estado
        if grabador:
            estado += f" · grabando ({grabador.escritos} líneas, marcador: {marcador.estado})"
        vista = tabla(visibles[:args.max_filas] if args.max_filas else visibles, estado, args.demo, error)
        if simuladores:
            por_token = {f.token_id: f for f in filas}
            for simulador in simuladores:
                simulador.paso(por_token, operaciones)
            vista = Group(vista, panel(simuladores))
        return vista, filas

    if not args.demo:
        buscar_partidos()
    fuente.iniciar()
    if marcador:
        marcador.iniciar()
    if not args.demo:
        completar_libros()

    if args.una_vez:
        time.sleep(3)  # dar tiempo a que lleguen los primeros libros
        consola.print(pantalla()[0])
        return

    try:
        with Live(pantalla()[0], console=consola, auto_refresh=False) as live:
            while True:
                time.sleep(0.5)
                if not args.demo and time.monotonic() - ultima_busqueda > BUSCAR_PARTIDOS_CADA:
                    buscar_partidos()
                if not args.demo and time.monotonic() - ultimo_completar > COMPLETAR_LIBROS_CADA:
                    completar_libros()
                vista, filas = pantalla()
                live.update(vista, refresh=True)
                if args.csv and time.monotonic() - ultimo_csv > args.csv_cada:
                    guardar_csv(args.csv, filas)
                    ultimo_csv = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        if grabador:
            grabador.cerrar()
    if simuladores:
        consola.print(panel(simuladores, max_cerradas=20))
        if any(s.cerradas for s in simuladores):
            consola.print(f"Operaciones guardadas en {args.papel_csv}. Resumen por día: python resumen_papel.py")


if __name__ == "__main__":
    main()
