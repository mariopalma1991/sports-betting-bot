"""Busca buenos candidatos para cobrar recompensas de liquidez en mercados deportivos de Polymarket.

Polymarket reparte una bolsa diaria por mercado entre quienes dejan órdenes límite cerca
del precio medio. Este programa busca los mercados de deportes con premio activo (partidos
que aún no empiezan y mercados de temporada), calcula los puntos de las órdenes que ya
compiten (el libro es público), estima qué parte de la bolsa te tocaría con tu capital y
muestra solo los que pasan los criterios:

    - libro con compradores y vendedores, con spread no mayor que la distancia máxima del premio
    - precio medio entre 0.15 y 0.85
    - el mínimo de acciones cabe en tu capital
    - cada dólar te pagaría al menos 2¢ al día y ganarías al menos 0.10 USD al día
    - si es un partido, falta al menos 1 hora para que empiece
    - el precio no se movió más de 3 veces la distancia máxima del premio en los últimos 7 días

Puntos de una orden (documentación de Polymarket):
    ((v - s) / v) ** 2 × acciones, con v = distancia máxima y s = distancia al precio medio, en ¢
    Solo un lado (precio medio entre 0.10 y 0.90): los puntos se dividen entre 3.
    Precio medio fuera de ese rango: solo cuenta si pones los dos lados.

Solo lee datos públicos: no usa llaves, no toca tu wallet y no coloca órdenes.

Uso:
    python recompensas.py                    # buenos candidatos de deportes y guarda recompensas.csv
    python recompensas.py --todos            # todos los mercados de deportes con premio y por qué se descartan
    python recompensas.py --capital 25,50    # capitales a simular (por defecto 25 y 50 USD)
    python recompensas.py --distancia 1      # tu orden a 1¢ del precio medio (por defecto)
    python recompensas.py --demo             # datos simulados, para ver cómo se ve
"""

from __future__ import annotations

import argparse
import csv
import math
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from rich.console import Console
from rich.table import Table
from rich.text import Text

from cuotas import obtener_libros
from nba_monitor import CLOB_URL, GAMMA_URL, _fecha, _json_list

RECOMPENSAS_CSV = Path("recompensas.csv")
FACTOR_UN_LADO = 3.0     # "c" en la documentación: un solo lado puntúa entre 3
RANGO_UN_LADO = (0.10, 0.90)
FIN_DE_PAGINAS = "LTE="

# Criterios de un buen candidato (se pueden cambiar con opciones del programa).
RANGO_PRECIO = (0.15, 0.85)   # con margen para no salirse de 0.10-0.90 si el precio se mueve
MIN_RENDIMIENTO = 0.02        # USD por cada dólar puesto, al día (2¢)
MIN_GANANCIA_DIA = 0.10       # USD por día
MIN_HORAS = 1.0               # horas antes del inicio de un partido
MAX_MOVIMIENTO = 3.0          # el precio se movió (máx − mín en 7 días) como mucho 3 veces la distancia máxima
DIAS_HISTORIA = 7
HISTORIA_POR_PEDIDO = 20      # máximo del endpoint /batch-prices-history


# --- Premios activos (CLOB) ----------------------------------------------------

@dataclass
class Premio:
    condition_id: str
    bolsa_diaria: float   # USD por día
    min_acciones: float
    max_distancia: float  # centavos


def bolsa_activa(item: dict, ahora: datetime) -> float:
    """Suma de las bolsas diarias vigentes hoy. Un mercado puede aparecer sin premio activo."""
    total = 0.0
    for config in item.get("rewards_config") or []:
        inicio, fin = _fecha(config.get("start_date")), _fecha(config.get("end_date"))
        if inicio and inicio > ahora:
            continue
        # Una fecha de fin sin hora cubre todo ese día.
        if fin and fin + (timedelta(days=1) if fin.time() == datetime.min.time() else timedelta()) < ahora:
            continue
        total += float(config.get("rate_per_day") or 0)
    if total == 0 and item.get("total_daily_rate") and not item.get("rewards_config"):
        total = float(item["total_daily_rate"])
    return total


def premio_de_item(item: dict, ahora: datetime) -> Premio | None:
    bolsa = bolsa_activa(item, ahora)
    if bolsa <= 0 or not item.get("condition_id"):
        return None
    return Premio(
        condition_id=str(item["condition_id"]),
        bolsa_diaria=bolsa,
        min_acciones=float(item.get("rewards_min_size") or 0),
        max_distancia=float(item.get("rewards_max_spread") or 0),
    )


def obtener_premios(sesion: requests.Session, ahora: datetime) -> dict[str, Premio]:
    premios: dict[str, Premio] = {}
    cursor = ""
    for _ in range(100):  # tope de seguridad por si el cursor nunca termina
        r = sesion.get(f"{CLOB_URL}/rewards/markets/current", params={"next_cursor": cursor} if cursor else None, timeout=15)
        r.raise_for_status()
        js = r.json()
        items = js.get("data") or [] if isinstance(js, dict) else js
        for item in items:
            p = premio_de_item(item, ahora)
            if p:
                premios[p.condition_id] = p
        cursor = js.get("next_cursor") if isinstance(js, dict) else None
        if not cursor or cursor == FIN_DE_PAGINAS or not items:
            break
    return premios


# --- Partidos (Gamma) ----------------------------------------------------------

@dataclass
class Partido:
    condition_id: str
    partido: str
    mercado: str
    resultados: list[str]
    token_id: str          # primer resultado; el libro del otro es el espejo
    inicio: datetime | None  # None: mercado de temporada (campeón, ascenso...), sin hora de partido
    slug: str = ""
    tokens: list[str] = field(default_factory=list)  # uno por resultado, en el mismo orden
    cierre: datetime | None = None  # cuándo cierra el mercado (endDate de Gamma)


def etiquetas_deportes(sesion: requests.Session) -> set[str]:
    """Ids de las etiquetas de Gamma que pertenecen a algún deporte (NBA, EPL, NFL, esports...)."""
    r = sesion.get(f"{GAMMA_URL}/sports", timeout=15)
    r.raise_for_status()
    ids = set()
    for deporte in r.json():
        ids.update(t.strip() for t in str(deporte.get("tags") or "").split(",") if t.strip())
    return ids


def _etiquetas(m: dict) -> list[dict]:
    etiquetas = list(m.get("tags") or [])
    for evento in m.get("events") or []:
        etiquetas.extend(evento.get("tags") or [])
    return [e for e in etiquetas if isinstance(e, dict)]


def es_deporte(m: dict, ids_deportes: set[str]) -> bool:
    """La hora de partido no basta: los mercados de conteo de posts también la traen."""
    if m.get("sportsMarketType"):
        return True
    return any(str(e.get("id")) in ids_deportes or e.get("slug") == "sports" for e in _etiquetas(m))


def partido_de_mercado(m: dict, ahora: datetime, ids_deportes: set[str]) -> Partido | None:
    """Mercados de deportes abiertos: partidos que no han empezado y mercados de temporada."""
    inicio = _fecha(m.get("gameStartTime"))
    tokens = _json_list(m.get("clobTokenIds"))
    if (inicio and inicio <= ahora) or m.get("closed") or not tokens or not es_deporte(m, ids_deportes):
        return None
    eventos = m.get("events") or []
    return Partido(
        condition_id=str(m.get("conditionId")),
        partido=(eventos[0].get("title") if eventos else None) or m.get("question") or "?",
        mercado=m.get("groupItemTitle") or m.get("sportsMarketType") or m.get("question") or "",
        resultados=[str(r) for r in _json_list(m.get("outcomes"))],
        token_id=str(tokens[0]),
        inicio=inicio,
        slug=str(m.get("slug") or ""),
        tokens=[str(t) for t in tokens],
        cierre=_fecha(m.get("endDate")),
    )


def obtener_partidos(sesion: requests.Session, condition_ids: list[str], ahora: datetime,
                     ids_deportes: set[str]) -> list[Partido]:
    partidos = []
    for i in range(0, len(condition_ids), 50):
        bloque = condition_ids[i:i + 50]
        r = sesion.get(f"{GAMMA_URL}/markets", params={"condition_ids": bloque, "limit": len(bloque),
                                                       "include_tag": "true"}, timeout=15)
        r.raise_for_status()
        for m in r.json():
            p = partido_de_mercado(m, ahora, ids_deportes)
            if p:
                partidos.append(p)
    return partidos


# --- Cálculo de puntos -----------------------------------------------------------

def puntos_por_cercania(distancia: float, max_distancia: float) -> float:
    """Factor de una orden según su distancia al precio medio, ambas en centavos."""
    if max_distancia <= 0 or distancia >= max_distancia:
        return 0.0
    return ((max_distancia - distancia) / max_distancia) ** 2


def puntos_combinados(compra: float, venta: float, medio: float) -> float:
    """Puntos de un participante con `compra` puntos de un lado y `venta` del otro."""
    if RANGO_UN_LADO[0] <= medio <= RANGO_UN_LADO[1]:
        return max(min(compra, venta), max(compra, venta) / FACTOR_UN_LADO)
    return min(compra, venta)


def precios_del_libro(libro: dict) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    compras = sorted(((float(n["price"]), float(n["size"])) for n in libro.get("bids") or []), reverse=True)
    ventas = sorted((float(n["price"]), float(n["size"])) for n in libro.get("asks") or [])
    return compras, ventas


@dataclass
class Competencia:
    medio: float
    mejor_compra: float
    mejor_venta: float
    tick: float
    puntos_compra: float
    puntos_venta: float

    @property
    def total(self) -> float:
        # El libro junta las órdenes por precio, así que no se sabe quién puso qué. Se supone
        # que cada rival cotiza parejo los dos lados: entonces la suma de sus puntos es el
        # promedio de los dos lados. Cuenta todas las órdenes, aunque alguna sea más chica que
        # el mínimo, así que la competencia sale de más y tu parte estimada, de menos.
        return (self.puntos_compra + self.puntos_venta) / 2


def competencia(libro: dict | None, premio: Premio) -> Competencia | None:
    if not libro:
        return None
    compras, ventas = precios_del_libro(libro)
    if not compras or not ventas:
        return None
    medio = (compras[0][0] + ventas[0][0]) / 2
    lado = lambda niveles: sum(puntos_por_cercania(abs(p - medio) * 100, premio.max_distancia) * c for p, c in niveles)
    return Competencia(
        medio=medio, mejor_compra=compras[0][0], mejor_venta=ventas[0][0],
        tick=float(libro.get("tick_size") or 0.01),
        puntos_compra=lado(compras), puntos_venta=lado(ventas),
    )


@dataclass
class Simulacion:
    capital: float
    lados: int             # 1 o 2
    acciones: float
    puntos: float
    parte: float           # fracción de la bolsa
    ganancia: float        # USD estimados hasta el inicio


def simular(comp: Competencia, premio: Premio, capital: float, distancia: float, horas: float) -> Simulacion | None:
    """La mejor forma de poner `capital`: un lado o los dos. None si no alcanza el mínimo."""
    # Tu compra a `distancia` ¢ abajo del medio, redondeada al tick y sin cruzar el libro.
    # Tu venta es una compra del otro resultado a 1 − precio, a la misma distancia arriba.
    paso = comp.tick
    compra = min(math.floor((comp.medio - distancia / 100) / paso + 1e-9) * paso, comp.mejor_venta - paso)
    venta = max(math.ceil((comp.medio + distancia / 100) / paso - 1e-9) * paso, comp.mejor_compra + paso)
    factor_c = puntos_por_cercania((comp.medio - compra) * 100, premio.max_distancia)
    factor_v = puntos_por_cercania((venta - comp.medio) * 100, premio.max_distancia)
    fraccion_dia = min(max(horas, 0), 24) / 24
    opciones = []
    # Dos lados: cada par cuesta compra + (1 − venta), casi 1 USD.
    costo_par = compra + (1 - venta)
    if costo_par > 0:
        n = capital / costo_par
        if n >= premio.min_acciones:
            opciones.append((2, n, puntos_combinados(factor_c * n, factor_v * n, comp.medio)))
    # Un lado: el más barato da más acciones por dólar.
    for precio, factor in ((compra, factor_c), (1 - venta, factor_v)):
        if precio > 0:
            n = capital / precio
            if n >= premio.min_acciones:
                opciones.append((1, n, puntos_combinados(factor * n, 0, comp.medio)))
    opciones = [o for o in opciones if o[2] > 0]
    if not opciones:
        return None
    lados, n, puntos = max(opciones, key=lambda o: o[2])
    parte = puntos / (comp.total + puntos)
    return Simulacion(capital=capital, lados=lados, acciones=n, puntos=puntos, parte=parte,
                      ganancia=parte * premio.bolsa_diaria * fraccion_dia)


@dataclass
class Fila:
    partido: str
    mercado: str
    inicio: datetime | None
    horas: float
    bolsa_diaria: float
    min_acciones: float
    max_distancia: float
    medio: float | None
    spread: float | None    # centavos
    competencia: float | None
    simulaciones: list[Simulacion | None]
    motivo: str = ""        # vacío si es buen candidato; si no, por qué se descarta
    condition_id: str = ""
    cierre: datetime | None = None
    historia: list[float] = field(default_factory=list)   # precios del primer resultado, cada hora, 7 días

    @property
    def movimiento(self) -> float | None:
        """Cuánto se movió el precio en la semana (máximo − mínimo), en centavos."""
        return (max(self.historia) - min(self.historia)) * 100 if len(self.historia) >= 2 else None

    @property
    def costo_minimo(self) -> float | None:
        """Lo que cuesta el mínimo de acciones del lado más barato."""
        return None if self.medio is None else self.min_acciones * min(self.medio, 1 - self.medio)

    @property
    def rendimiento(self) -> float:
        """Ganancia estimada por dólar con el capital más grande que alcance."""
        mejores = [s.ganancia / s.capital for s in self.simulaciones if s]
        return max(mejores, default=-1.0)


def analizar(partidos: list[Partido], premios: dict[str, Premio], libros: dict[str, dict],
             capitales: list[float], distancia: float, ahora: datetime,
             historias: dict[str, list[float]] | None = None) -> list[Fila]:
    filas = []
    for p in partidos:
        premio = premios.get(p.condition_id)
        if not premio:
            continue
        # Un mercado de temporada no tiene hora de inicio: se cuenta el día completo.
        horas = 24.0 if p.inicio is None else (p.inicio - ahora).total_seconds() / 3600
        comp = competencia(libros.get(p.token_id), premio)
        sims = [simular(comp, premio, c, distancia, horas) if comp else None for c in capitales]
        filas.append(Fila(
            partido=p.partido, mercado=p.mercado, inicio=p.inicio, horas=horas,
            bolsa_diaria=premio.bolsa_diaria, min_acciones=premio.min_acciones,
            max_distancia=premio.max_distancia, medio=comp.medio if comp else None,
            spread=(comp.mejor_venta - comp.mejor_compra) * 100 if comp else None,
            competencia=comp.total if comp else None, simulaciones=sims, condition_id=p.condition_id,
            cierre=p.cierre, historia=(historias or {}).get(p.token_id, []),
        ))
    return sorted(filas, key=lambda f: f.rendimiento, reverse=True)


def motivo_descarte(f: Fila, rango_precio=RANGO_PRECIO, min_rendimiento=MIN_RENDIMIENTO,
                    min_ganancia_dia=MIN_GANANCIA_DIA, min_horas=MIN_HORAS, max_movimiento=MAX_MOVIMIENTO) -> str:
    """Por qué un mercado no es buen candidato, o "" si pasa todos los criterios."""
    if f.medio is None:
        return "libro de un solo lado"
    if f.spread > f.max_distancia + 1e-9:
        return f"spread {f.spread:.1f}¢ > {f.max_distancia:g}¢"
    if not rango_precio[0] <= f.medio <= rango_precio[1]:
        return "precio cerca de 0 o 1"
    if f.inicio is not None and f.horas < min_horas:
        return "empieza pronto"
    if f.movimiento is not None and f.movimiento > max_movimiento * f.max_distancia:
        return f"precio inestable (se movió {f.movimiento:.0f}¢ en {DIAS_HISTORIA} días)"
    sims = [s for s in f.simulaciones if s]
    if not sims:
        return "mínimo muy caro"
    if f.rendimiento < min_rendimiento:
        return f"rinde poco ({f.rendimiento * 100:.1f}¢ por $1)"
    if max(s.ganancia for s in sims) < min_ganancia_dia:
        return "bolsa muy chica"
    return ""


def obtener_historias(sesion: requests.Session, token_ids: list[str], ahora: datetime) -> dict[str, list[float]]:
    """Precio de cada token, cada hora, en los últimos 7 días. Si falla, sigue sin historia."""
    historias: dict[str, list[float]] = {}
    desde = int((ahora - timedelta(days=DIAS_HISTORIA)).timestamp())
    for i in range(0, len(token_ids), HISTORIA_POR_PEDIDO):
        bloque = token_ids[i:i + HISTORIA_POR_PEDIDO]
        try:
            r = sesion.post(f"{CLOB_URL}/batch-prices-history", timeout=15,
                            json={"markets": bloque, "start_ts": desde, "fidelity": 60})
            r.raise_for_status()
            datos = r.json().get("history") or {}
        except (requests.RequestException, ValueError, AttributeError):
            continue
        for tok, puntos in datos.items():
            precios = [float(x["p"]) for x in sorted(puntos or [], key=lambda x: x.get("t", 0)) if "p" in x]
            if precios:
                historias[str(tok)] = precios
    return historias


def historias_demo(partidos: list[Partido], libros: dict[str, dict]) -> dict[str, list[float]]:
    """Caminatas al azar alrededor del precio actual; América vs. Chivas brinca como los Devils."""
    rnd = random.Random(11)
    historias = {}
    for p in partidos:
        b = libros.get(p.token_id) or {}
        try:
            medio = (float(b["bids"][0]["price"]) + float(b["asks"][0]["price"])) / 2
        except (KeyError, IndexError):
            continue
        salto = 0.01 if p.condition_id == "c31" else 0.002
        precio, serie = medio, []
        for _ in range(DIAS_HISTORIA * 24):
            precio = min(0.99, max(0.01, precio + rnd.gauss(0, salto)))
            serie.append(round(precio, 3))
        serie[-1] = medio
        historias[p.token_id] = serie
    return historias


# --- Datos simulados ---------------------------------------------------------------

def datos_demo(ahora: datetime) -> tuple[list[Partido], dict[str, Premio], dict[str, dict]]:
    rnd = random.Random(7)
    juegos = [("Lakers vs. Suns", "NBA"), ("Celtics vs. Knicks", "NBA"), ("Chiefs vs. Bills", "NFL"),
              ("América vs. Chivas", "Liga MX"), ("Arsenal vs. Chelsea", "EPL"), ("Dodgers vs. Yankees", "MLB")]
    partidos, premios, libros = [], {}, {}
    for i, (juego, _liga) in enumerate(juegos):
        for j, tipo in enumerate(("Ganador", "Spread", "Totales")):
            cid, tok = f"c{i}{j}", f"t{i}{j}"
            partidos.append(Partido(cid, juego, tipo, ["Sí", "No"], tok, ahora + timedelta(hours=rnd.uniform(1, 20))))
            premios[cid] = Premio(cid, rnd.choice([10, 25, 50, 100, 250]), rnd.choice([20, 50, 100, 200]), rnd.choice([2, 3, 4.5]))
            medio = round(rnd.uniform(0.15, 0.85), 2)
            niveles = lambda signo: [{"price": f"{medio + signo * (k + 0.5) / 100:.3f}", "size": str(rnd.randint(0, 4) * 500)}
                                     for k in range(4)]
            libros[tok] = {"bids": niveles(-1), "asks": niveles(1), "tick_size": "0.001"}
    # Mercados de temporada: uno con buen libro y otro con un spread enorme.
    partidos.append(Partido("f1", "¿West Ham asciende a la Premier?", "Temporada", ["Yes", "No"], "tf1", None))
    premios["f1"] = Premio("f1", 2, 50, 4)
    libros["tf1"] = {"bids": [{"price": "0.76", "size": "10"}], "asks": [{"price": "0.78", "size": "10"}], "tick_size": "0.01"}
    partidos.append(Partido("f2", "¿Bolton asciende a la Premier?", "Temporada", ["Yes", "No"], "tf2", None))
    premios["f2"] = Premio("f2", 2, 50, 4)
    libros["tf2"] = {"bids": [{"price": "0.061", "size": "28"}], "asks": [{"price": "0.529", "size": "23"}], "tick_size": "0.001"}
    for p in partidos:
        p.cierre = p.inicio + timedelta(hours=3) if p.inicio else ahora + timedelta(days=180)
    return partidos, premios, libros


# --- Salida --------------------------------------------------------------------

def _sim(s: Simulacion | None) -> Text:
    if s is None:
        return Text("no alcanza", style="dim")
    lado = "2 lados" if s.lados == 2 else "1 lado"
    estilo = "bold green" if s.ganancia >= 0.5 else "green" if s.ganancia >= 0.1 else ""
    return Text(f"${s.ganancia:.2f} · {s.parte:.0%} · {lado}", style=estilo)


def tabla(filas: list[Fila], capitales: list[float], distancia: float, max_filas: int, demo: bool,
          todos: bool = False) -> Table:
    titulo = ("Recompensas de liquidez · " + ("todos los mercados de deportes" if todos else "buenos candidatos de deportes")
              + (" (DEMO)" if demo else ""))
    pie = (f"Tu orden a {distancia:g}¢ del precio medio; en partidos, cancelada al inicio. "
           "Celdas: USD estimados (partidos: hasta el inicio; temporada: un día) · tu parte de la bolsa · lados. "
           "Es una estimación: abre el libro antes de poner la orden.")
    t = Table(title=titulo, caption=pie, header_style="bold", expand=True)
    columnas = ["Mercado", "Tipo", "Inicio", "Bolsa/día", "Mín. acc.", "Costo mín.", "Máx. dist.", "Medio", "Spread", "Competencia"]
    for col in columnas:
        t.add_column(col, justify="left" if col in ("Mercado", "Tipo") else "right")
    for c in capitales:
        t.add_column(f"Con {c:g} USD", justify="right")
    if todos:
        t.add_column("Descarte", justify="left")
    for f in filas[:max_filas]:
        inicio = "temporada" if f.inicio is None else f"{f.inicio.astimezone():%d/%m %H:%M} ({f.horas:.0f} h)"
        fila = [
            f.partido, f.mercado, inicio,
            f"${f.bolsa_diaria:,.0f}", f"{f.min_acciones:g}",
            "—" if f.costo_minimo is None else f"${f.costo_minimo:,.2f}", f"{f.max_distancia:g}¢",
            "—" if f.medio is None else f"{f.medio:.3f}",
            "—" if f.spread is None else f"{f.spread:.1f}¢",
            "—" if f.competencia is None else f"{f.competencia:,.0f} pts",
            *[_sim(s) for s in f.simulaciones],
        ]
        if todos:
            fila.append(Text(f.motivo or "✓ candidato", style="red" if f.motivo else "bold green"))
        t.add_row(*fila)
    if not filas:
        t.add_row("Ningún mercado de deportes pasa los criterios ahora (prueba con --todos)",
                  *[""] * (len(columnas) - 1 + len(capitales) + (1 if todos else 0)))
    return t


def guardar_csv(ruta: Path, filas: list[Fila], ahora: datetime) -> None:
    nuevo = not ruta.exists()
    with ruta.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if nuevo:
            w.writerow(["momento", "partido", "mercado", "inicio", "horas", "bolsa_diaria", "min_acciones",
                        "max_distancia", "medio", "competencia", "capital", "lados", "acciones", "parte", "ganancia",
                        "spread", "descarte"])
        for f in filas:
            for s in f.simulaciones:
                base = [ahora.isoformat(timespec="seconds"), f.partido, f.mercado, f.inicio.isoformat() if f.inicio else "temporada", f"{f.horas:.2f}",
                        f.bolsa_diaria, f.min_acciones, f.max_distancia,
                        "" if f.medio is None else f"{f.medio:.4f}",
                        "" if f.competencia is None else f"{f.competencia:.1f}"]
                extra = ["" if f.spread is None else f"{f.spread:.2f}", f.motivo]
                if s is None:
                    w.writerow(base + ["", "", "", "", ""] + extra)
                else:
                    w.writerow(base + [s.capital, s.lados, f"{s.acciones:.1f}", f"{s.parte:.4f}", f"{s.ganancia:.4f}"] + extra)


def main() -> None:
    parser = argparse.ArgumentParser(description="Estima las recompensas de liquidez en partidos de Polymarket")
    parser.add_argument("--capital", default="25,50", help="capitales a simular en USD, separados por coma (por defecto 25,50)")
    parser.add_argument("--distancia", type=float, default=1.0, help="centavos entre tu orden y el precio medio (por defecto 1)")
    parser.add_argument("--csv", type=Path, default=RECOMPENSAS_CSV, help="archivo donde se agrega cada corrida (por defecto recompensas.csv)")
    parser.add_argument("--max-filas", type=int, default=30)
    parser.add_argument("--todos", action="store_true", help="mostrar todos los mercados de deportes con premio y por qué se descartan")
    parser.add_argument("--min-rendimiento", type=float, default=MIN_RENDIMIENTO * 100,
                        help="centavos mínimos por dólar al día (por defecto 2)")
    parser.add_argument("--min-ganancia", type=float, default=MIN_GANANCIA_DIA, help="USD mínimos por día (por defecto 0.10)")
    parser.add_argument("--demo", action="store_true", help="datos simulados, sin conectarse a Polymarket")
    args = parser.parse_args()
    capitales = [float(c) for c in args.capital.split(",") if c.strip()]

    consola = Console()
    ahora = datetime.now(timezone.utc)
    if args.demo:
        partidos, premios, libros = datos_demo(ahora)
    else:
        sesion = requests.Session()
        with consola.status("Buscando premios activos..."):
            premios = obtener_premios(sesion, ahora)
        with consola.status(f"{len(premios)} mercados con premio. Buscando cuáles son de deportes..."):
            partidos = obtener_partidos(sesion, list(premios), ahora, etiquetas_deportes(sesion))
        with consola.status(f"{len(partidos)} mercados de deportes. Leyendo libros de órdenes..."):
            libros = obtener_libros(sesion, [p.token_id for p in partidos])
        with consola.status("Leyendo cómo se movió el precio en la semana..."):
            historias = obtener_historias(sesion, [p.token_id for p in partidos], ahora)
    if args.demo:
        historias = historias_demo(partidos, libros)

    filas = analizar(partidos, premios, libros, capitales, args.distancia, ahora, historias)
    for f in filas:
        f.motivo = motivo_descarte(f, min_rendimiento=args.min_rendimiento / 100, min_ganancia_dia=args.min_ganancia)
    candidatos = [f for f in filas if not f.motivo]
    consola.print(tabla(filas if args.todos else candidatos, capitales, args.distancia, args.max_filas, args.demo, args.todos))
    consola.print(f"[dim]{len(premios)} mercados con premio activo; {len(filas)} son de deportes; "
                  f"{len(candidatos)} pasan los criterios.[/dim]")
    if not args.demo:
        guardar_csv(args.csv, filas, ahora)
        consola.print(f"[dim]Guardado en {args.csv}.[/dim]")


if __name__ == "__main__":
    main()
