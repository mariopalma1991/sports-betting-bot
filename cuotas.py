"""Compara los precios de Polymarket con los momios de una casa de apuestas (Pinnacle por defecto).

La casa da un momio por resultado; se pasa a probabilidad (1 / momio) y se le quita
el margen de la casa repartiéndolo en proporción. Si en Polymarket puedes comprar
ese resultado más barato que esa probabilidad "justa", hay ventaja.

Momios: The Odds API (https://the-odds-api.com). Necesitas una llave gratis y ponerla
en la variable de entorno ODDS_API_KEY o pasarla con --llave. No la pegues en ningún chat.

Cada oportunidad se apuesta en papel con dos estrategias en paralelo:
  - mantener: comprar y esperar al final del partido.
  - vender_justo: comprar y dejar una venta al precio justo de la casa; si nunca se llena,
    se queda hasta el final como "mantener".

Uso:
    python cuotas.py                     # tabla en vivo + apuestas en papel en valor_apuestas.csv
    python cuotas.py --mercados h2h,spreads,totals --min-ventaja 0.04
    python cuotas.py --liquidar          # revisa qué apuestas ya se resolvieron y cuánto ganaste

Solo lee datos y registra apuestas ficticias: no toca tu wallet ni coloca órdenes.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from nba_monitor import CLOB_URL, GAMMA_URL, NBA_TAG_ID, _fecha, _json_list, es_partido

ODDS_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
APUESTAS_CSV = Path("valor_apuestas.csv")


# --- Momios de la casa --------------------------------------------------------

def sin_margen(momios: list[float]) -> list[float]:
    """Momios decimales -> probabilidades sin el margen de la casa (suman 1)."""
    implicitas = [1 / m for m in momios]
    total = sum(implicitas)
    return [p / total for p in implicitas]


def mismo_equipo(corto: str, completo: str) -> bool:
    """"Lakers" coincide con "Los Angeles Lakers"; "76ers" con "Philadelphia 76ers"."""
    corto, completo = corto.strip().lower(), completo.strip().lower()
    return bool(corto) and (completo == corto or completo.endswith(" " + corto))


@dataclass
class Linea:
    """Probabilidad justa de un resultado según la casa."""
    resultado: str          # nombre completo del equipo, "Over" o "Under"
    punto: float | None     # línea del spread o del total; None en ganador
    momio: float
    prob: float


def lineas_de_evento(evento: dict, casa: str) -> dict[str, list[Linea]]:
    """Mercado ("h2h", "spreads", "totals") -> líneas sin margen de la casa elegida."""
    for libro in evento.get("bookmakers") or []:
        if libro.get("key") != casa:
            continue
        salida: dict[str, list[Linea]] = {}
        for mercado in libro.get("markets") or []:
            resultados = mercado.get("outcomes") or []
            if len(resultados) != 2:
                continue
            probs = sin_margen([float(r["price"]) for r in resultados])
            salida[mercado["key"]] = [
                Linea(r["name"], float(r["point"]) if r.get("point") is not None else None, float(r["price"]), p)
                for r, p in zip(resultados, probs)
            ]
        return salida
    return {}


def obtener_momios(sesion: requests.Session, llave: str, mercados: str, casa: str) -> tuple[list[dict], str | None]:
    r = sesion.get(ODDS_URL, params={
        "apiKey": llave, "bookmakers": casa, "markets": mercados, "oddsFormat": "decimal",
    }, timeout=20)
    r.raise_for_status()
    return r.json(), r.headers.get("x-requests-remaining")


# --- Mercados de Polymarket ---------------------------------------------------

@dataclass
class MercadoPM:
    id: str
    partido: str            # "Lakers vs. Celtics"
    titulo: str             # "Ganador", "Spread -4.5", "O/U 224.5"
    tipo: str               # "h2h", "spreads" o "totals"
    punto: float | None     # línea del primer resultado (spread) o del total
    resultados: list[str]   # p. ej. ["Lakers", "Celtics"] u ["Over", "Under"]
    token_id: str           # token del primer resultado
    inicio: datetime | None


def _ultimo_numero(texto: str) -> float | None:
    numeros = re.findall(r"[-+]?\d+(?:\.\d+)?", texto or "")
    return float(numeros[-1]) if numeros else None


def tipo_de_mercado(m: dict, resultados: list[str]) -> str | None:
    tipo = (m.get("sportsMarketType") or "").lower()
    titulo = m.get("groupItemTitle") or m.get("question") or ""
    if tipo == "moneyline":
        return "h2h"
    if tipo in ("spreads", "totals"):
        return tipo
    # Sin sportsMarketType: se deduce del título. Las props de jugadores ("X: Points O/U 4.5",
    # con resultados Yes/No) quedan fuera.
    if titulo.startswith("Spread"):
        return "spreads"
    if titulo.startswith("O/U") and [r.lower() for r in resultados] == ["over", "under"]:
        return "totals"
    return None


def mercados_pm_de_evento(evento: dict) -> list[MercadoPM]:
    salida = []
    for m in evento.get("markets") or []:
        if m.get("closed") or not m.get("enableOrderBook", True):
            continue
        tokens = _json_list(m.get("clobTokenIds"))
        resultados = [str(r) for r in _json_list(m.get("outcomes"))]
        tipo = tipo_de_mercado(m, resultados)
        if not tokens or len(resultados) != 2 or tipo is None:
            continue
        titulo = m.get("groupItemTitle") or ("Ganador" if tipo == "h2h" else m.get("question") or "")
        salida.append(MercadoPM(
            id=str(m.get("id")), partido=evento.get("title") or "", titulo=titulo, tipo=tipo,
            punto=None if tipo == "h2h" else _ultimo_numero(titulo),
            resultados=resultados, token_id=str(tokens[0]),
            inicio=_fecha(m.get("gameStartTime") or evento.get("startDate")),
        ))
    return salida


def obtener_mercados_pm(sesion: requests.Session) -> list[MercadoPM]:
    mercados: list[MercadoPM] = []
    offset = 0
    while True:
        r = sesion.get(f"{GAMMA_URL}/events", params={
            "tag_id": NBA_TAG_ID, "active": "true", "closed": "false", "limit": 100, "offset": offset,
        }, timeout=15)
        r.raise_for_status()
        eventos = r.json()
        for evento in eventos:
            if es_partido(evento):
                mercados.extend(mercados_pm_de_evento(evento))
        if len(eventos) < 100:
            return mercados
        offset += 100


def obtener_libros(sesion: requests.Session, tokens: list[str]) -> dict[str, dict]:
    libros: dict[str, dict] = {}
    for i in range(0, len(tokens), 50):
        bloque = tokens[i:i + 50]
        r = sesion.post(f"{CLOB_URL}/books", json=[{"token_id": t} for t in bloque], timeout=15)
        if r.ok:
            encontrados = r.json()
        else:
            # Un token sin libro hace fallar todo el bloque: se piden de uno en uno.
            encontrados = []
            for t in bloque:
                uno = sesion.get(f"{CLOB_URL}/book", params={"token_id": t}, timeout=15)
                if uno.ok:
                    encontrados.append(uno.json())
        for libro in encontrados:
            libros[str(libro.get("asset_id"))] = libro
    return libros


# --- Comparación --------------------------------------------------------------

@dataclass
class Oportunidad:
    mercado_id: str
    partido: str
    titulo: str
    lado: str
    indice: int             # 0 o 1: qué resultado del mercado de Polymarket se compra
    inicio: datetime | None
    momio: float
    prob_justa: float
    precio: float | None    # lo que cuesta comprar ese lado en Polymarket (mejor venta)
    disponible: float       # dólares disponibles a ese precio

    @property
    def ventaja(self) -> float | None:
        return None if self.precio is None else self.prob_justa - self.precio

    @property
    def valor_esperado(self) -> float | None:
        """Ganancia esperada por cada dólar apostado."""
        return None if not self.precio else self.prob_justa / self.precio - 1


def evento_de_partido(partido: str, inicio: datetime | None, eventos: list[dict]) -> dict | None:
    equipos = [e.strip() for e in partido.split(" vs. ")]
    if len(equipos) != 2:
        return None
    for evento in eventos:
        casa_local, visita = evento.get("home_team", ""), evento.get("away_team", "")
        if not all(any(mismo_equipo(e, n) for n in (casa_local, visita)) for e in equipos):
            continue
        comienzo = _fecha(evento.get("commence_time"))
        if inicio and comienzo and abs(comienzo - inicio) > timedelta(hours=18):
            continue  # mismos equipos, otro día
        return evento
    return None


def prob_de_lado(m: MercadoPM, indice: int, lineas: list[Linea]) -> Linea | None:
    nombre = m.resultados[indice]
    for linea in lineas:
        if m.tipo == "totals":
            if linea.resultado.lower() == nombre.lower() and m.punto is not None \
                    and linea.punto is not None and abs(linea.punto - m.punto) < 1e-6:
                return linea
        elif mismo_equipo(nombre, linea.resultado):
            if m.tipo == "h2h":
                return linea
            # Spread: la línea del título es del primer resultado; el segundo lleva la contraria.
            punto = m.punto if indice == 0 else (-m.punto if m.punto is not None else None)
            if punto is not None and linea.punto is not None and abs(linea.punto - punto) < 1e-6:
                return linea
    return None


def precio_de_compra(libro: dict | None, indice: int) -> tuple[float | None, float]:
    """Precio y dólares disponibles para comprar el resultado `indice` usando el libro del primero.

    Comprar el primero = tomar la mejor venta. Comprar el segundo = 1 - mejor compra del primero
    (en Polymarket los dos lados se cruzan entre sí).
    """
    if not libro:
        return None, 0.0
    if indice == 0:
        asks = [(float(n["price"]), float(n["size"])) for n in libro.get("asks") or []]
        if not asks:
            return None, 0.0
        precio, cantidad = min(asks)
        return precio, precio * cantidad
    bids = [(float(n["price"]), float(n["size"])) for n in libro.get("bids") or []]
    if not bids:
        return None, 0.0
    mejor, cantidad = max(bids)
    precio = round(1 - mejor, 4)
    return precio, precio * cantidad


def precio_de_venta(libro: dict | None, indice: int) -> float | None:
    """Mejor precio al que alguien compraría ahora el resultado `indice` (usando el libro del primero)."""
    if not libro:
        return None
    if indice == 0:
        bids = [float(n["price"]) for n in libro.get("bids") or []]
        return max(bids) if bids else None
    asks = [float(n["price"]) for n in libro.get("asks") or []]
    return round(1 - min(asks), 4) if asks else None


def comparar(mercados: list[MercadoPM], eventos: list[dict], libros: dict[str, dict], casa: str) -> list[Oportunidad]:
    salida = []
    for m in mercados:
        evento = evento_de_partido(m.partido, m.inicio, eventos)
        if evento is None:
            continue
        lineas = lineas_de_evento(evento, casa).get(m.tipo)
        if not lineas:
            continue
        for indice in (0, 1):
            linea = prob_de_lado(m, indice, lineas)
            if linea is None:
                continue
            precio, disponible = precio_de_compra(libros.get(m.token_id), indice)
            lado = m.resultados[indice]
            if m.tipo != "h2h" and m.punto is not None:
                punto = m.punto if indice == 0 or m.tipo == "totals" else -m.punto
                lado = f"{lado} {punto:+g}" if m.tipo == "spreads" else f"{lado} {punto:g}"
            salida.append(Oportunidad(m.id, m.partido, m.titulo, lado, indice, m.inicio,
                                      linea.momio, linea.prob, precio, disponible))
    return sorted(salida, key=lambda o: -(o.ventaja if o.ventaja is not None else -1))


# --- Apuestas en papel --------------------------------------------------------

ESTRATEGIAS = ("mantener", "vender_justo")

@dataclass
class Apuesta:
    momento: str
    mercado_id: str
    partido: str
    titulo: str
    lado: str
    indice: int
    inicio: str
    momio: float
    prob_justa: float
    precio: float
    monto: float
    resultado: str = ""     # "ganada", "perdida", "vendida" o vacío si no se ha resuelto
    ganancia: str = ""
    estrategia: str = "mantener"  # los CSV anteriores solo tenían esta
    venta: str = ""         # vender_justo: precio de la orden de venta
    vendida: str = ""       # vender_justo: hora en que se llenó la venta


def leer_apuestas(ruta: Path) -> list[Apuesta]:
    if not ruta.exists():
        return []
    with ruta.open(encoding="utf-8") as archivo:
        tipos = {f.name: f.type for f in fields(Apuesta)}
        apuestas = []
        for fila in csv.DictReader(archivo):
            for clave in ("momio", "prob_justa", "precio", "monto"):
                fila[clave] = float(fila[clave])
            fila["indice"] = int(fila["indice"])
            apuestas.append(Apuesta(**{k: v for k, v in fila.items() if k in tipos}))
        return apuestas


def escribir_apuestas(ruta: Path, apuestas: list[Apuesta]) -> None:
    with ruta.open("w", newline="", encoding="utf-8") as archivo:
        escritor = csv.DictWriter(archivo, fieldnames=[f.name for f in fields(Apuesta)])
        escritor.writeheader()
        for a in apuestas:
            escritor.writerow(asdict(a))


def registrar(oportunidades: list[Oportunidad], apuestas: list[Apuesta], min_ventaja: float,
              min_disponible: float, monto: float, ahora: datetime) -> list[Apuesta]:
    """Agrega una apuesta en papel por cada lado con ventaja suficiente, antes del partido y una sola vez."""
    ya = {(a.mercado_id, a.indice, a.estrategia) for a in apuestas}
    nuevas = []
    for o in oportunidades:
        if (o.ventaja is None or o.ventaja < min_ventaja - 1e-9 or o.disponible < min_disponible
                or (o.inicio is not None and o.inicio <= ahora)):
            continue
        for estrategia in ESTRATEGIAS:
            if (o.mercado_id, o.indice, estrategia) in ya:
                continue
            nuevas.append(Apuesta(
                momento=ahora.astimezone().strftime("%Y-%m-%d %H:%M:%S"), mercado_id=o.mercado_id,
                partido=o.partido, titulo=o.titulo, lado=o.lado, indice=o.indice,
                inicio=o.inicio.isoformat() if o.inicio else "", momio=o.momio,
                prob_justa=round(o.prob_justa, 4), precio=o.precio, monto=monto, estrategia=estrategia,
                venta=f"{precio_objetivo(o.prob_justa, o.precio):.2f}" if estrategia == "vender_justo" else "",
            ))
            ya.add((o.mercado_id, o.indice, estrategia))
    return nuevas


def precio_objetivo(prob_justa: float, compra: float) -> float:
    """Venta al precio justo redondeado al centavo, y siempre al menos 1¢ arriba de la compra."""
    return max(round(prob_justa, 2), round(compra + 0.01, 2))


def revisar_ventas(apuestas: list[Apuesta], libros_por_mercado: dict[str, dict], ahora: datetime,
                   comision: float = 0.0) -> int:
    """Marca como vendidas las apuestas vender_justo cuyo precio de venta ya alguien paga. Devuelve cuántas."""
    cambiadas = 0
    for a in apuestas:
        if a.estrategia != "vender_justo" or a.resultado or not a.venta:
            continue
        mejor = precio_de_venta(libros_por_mercado.get(a.mercado_id), a.indice)
        venta = float(a.venta)
        if mejor is None or mejor < venta - 1e-9:
            continue
        # La venta es límite (maker): no paga comisión; la compra sí.
        a.resultado = "vendida"
        a.ganancia = f"{a.monto / a.precio * venta - a.monto - a.monto * comision:.4f}"
        a.vendida = ahora.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        cambiadas += 1
    return cambiadas


def liquidar(apuestas: list[Apuesta], mercados_cerrados: dict[str, dict], comision: float = 0.0) -> int:
    """Llena resultado y ganancia de las apuestas cuyo mercado ya se resolvió. Devuelve cuántas cambió."""
    cambiadas = 0
    for a in apuestas:
        m = mercados_cerrados.get(a.mercado_id)
        if a.resultado or not m or not m.get("closed"):
            continue
        pagos = [float(p) for p in _json_list(m.get("outcomePrices"))]
        if len(pagos) != 2 or max(pagos) < 0.99:
            continue  # cerrado pero sin resolver todavía
        acciones = a.monto / a.precio
        cobro = acciones * pagos[a.indice]
        ganancia = cobro - a.monto - a.monto * comision
        a.resultado = "ganada" if pagos[a.indice] >= 0.99 else "perdida"
        a.ganancia = f"{ganancia:.4f}"
        cambiadas += 1
    return cambiadas


def obtener_mercado(sesion: requests.Session, mercado_id: str) -> dict | None:
    r = sesion.get(f"{GAMMA_URL}/markets/{mercado_id}", timeout=15)
    if r.ok:
        return r.json()
    r = sesion.get(f"{GAMMA_URL}/markets", params={"id": mercado_id}, timeout=15)
    if r.ok and r.json():
        return r.json()[0]
    return None


# --- Pantalla -----------------------------------------------------------------

def _p(v: float | None) -> str:
    return "—" if v is None else (f"{v:.2f}" if abs(v - round(v, 2)) < 1e-9 else f"{v:.3f}")


def tabla_oportunidades(ops: list[Oportunidad], min_ventaja: float, casa: str, estado: str, max_filas: int) -> Table:
    t = Table(title=f"Polymarket vs. {casa.title()} · NBA", caption=estado, header_style="bold", expand=True)
    for col in ("Partido", "Mercado", "Lado", "Inicio", "Momio", "Prob. justa", "Polymarket", "Ventaja", "Valor esp.", "Disponible"):
        t.add_column(col, justify="left" if col in ("Partido", "Mercado", "Lado") else "right")
    for o in ops[:max_filas]:
        v = o.ventaja
        estilo = "bold green" if v is not None and v >= min_ventaja else "red" if v is not None and v < 0 else ""
        t.add_row(
            o.partido, o.titulo, o.lado, f"{o.inicio.astimezone():%d/%m %H:%M}" if o.inicio else "—",
            f"{o.momio:.2f}", f"{o.prob_justa:.1%}", _p(o.precio),
            Text("—" if v is None else f"{v * 100:+.1f}¢", style=estilo),
            "—" if o.valor_esperado is None else f"{o.valor_esperado:+.1%}",
            f"${o.disponible:,.0f}",
        )
    if not ops:
        t.add_row("Sin mercados emparejados todavía", "", "", "", "", "", "", "", "", "")
    return t


def tabla_apuestas(apuestas: list[Apuesta], ultimas: int = 8) -> Group:
    resueltas = [a for a in apuestas if a.resultado]
    ganancia = sum(float(a.ganancia) for a in resueltas)
    ganadas = sum(a.resultado == "ganada" for a in resueltas)
    esperado = sum(a.monto * (a.prob_justa / a.precio - 1) for a in apuestas)
    resumen = Text.assemble(
        ("APUESTAS EN PAPEL", "bold magenta"),
        f"  {len(apuestas)} registradas, {len(resueltas)} resueltas ({ganadas} ganadas)  ",
        "Ganancia real: ", Text(f"{ganancia:+.2f} USD", style="green" if ganancia > 0 else "red" if ganancia < 0 else ""),
        f"  Ganancia esperada según la casa: {esperado:+.2f} USD",
    )
    lineas = [resumen]
    for estrategia in ESTRATEGIAS:
        suyas = [a for a in apuestas if a.estrategia == estrategia]
        if not suyas:
            continue
        cerradas = [a for a in suyas if a.resultado]
        g = sum(float(a.ganancia) for a in cerradas)
        vendidas = sum(a.resultado == "vendida" for a in cerradas)
        lineas.append(Text.assemble(
            f"  {estrategia:<13}", f"{len(suyas)} apuestas, {len(cerradas)} cerradas",
            f" ({vendidas} vendidas a precio justo)" if estrategia == "vender_justo" else "",
            "  Ganancia: ", Text(f"{g:+.2f} USD", style="green" if g > 0 else "red" if g < 0 else ""),
        ))
    t = Table(expand=True, header_style="bold")
    for col in ("Hora", "Estrategia", "Partido", "Mercado", "Lado", "Precio", "Venta", "Prob. justa", "Monto", "Resultado"):
        t.add_column(col, justify="left" if col in ("Estrategia", "Partido", "Mercado", "Lado", "Resultado") else "right")
    for a in apuestas[-ultimas:][::-1]:
        res = a.resultado or "pendiente"
        t.add_row(a.momento[11:16], a.estrategia, a.partido, a.titulo, a.lado, _p(a.precio),
                  a.venta or "—", f"{a.prob_justa:.1%}", f"${a.monto:.2f}",
                  Text(f"{res} {a.ganancia and float(a.ganancia):+.2f}" if a.resultado else res,
                       style="green" if res in ("ganada", "vendida") and float(a.ganancia or 0) > 0
                       else "red" if res in ("perdida", "vendida") else "dim"))
    return Group(*lineas, t)


# --- Programa -----------------------------------------------------------------

def correr_liquidar(args, consola: Console, sesion: requests.Session) -> None:
    apuestas = leer_apuestas(args.csv)
    pendientes = {a.mercado_id for a in apuestas if not a.resultado}
    cerrados = {}
    for mid in pendientes:
        m = obtener_mercado(sesion, mid)
        if m:
            cerrados[mid] = m
    cambiadas = liquidar(apuestas, cerrados, args.comision)
    if cambiadas:
        escribir_apuestas(args.csv, apuestas)
    consola.print(f"Se resolvieron {cambiadas} apuestas nuevas.")
    consola.print(tabla_apuestas(apuestas, ultimas=len(apuestas)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket vs. casa de apuestas para la NBA")
    parser.add_argument("--llave", default=os.environ.get("ODDS_API_KEY"), help="llave de The Odds API (o variable ODDS_API_KEY)")
    parser.add_argument("--casa", default="pinnacle", help="casa de referencia en The Odds API (por defecto pinnacle)")
    parser.add_argument("--mercados", default="h2h", help="h2h, spreads, totals separados por coma (cada uno gasta 1 crédito por consulta)")
    parser.add_argument("--momios-cada", type=float, default=600, help="segundos entre consultas de momios (por defecto 600)")
    parser.add_argument("--precios-cada", type=float, default=30, help="segundos entre consultas a Polymarket (por defecto 30)")
    parser.add_argument("--min-ventaja", type=float, default=0.03, help="ventaja mínima para apostar en papel (por defecto 0.03 = 3¢)")
    parser.add_argument("--min-disponible", type=float, default=5, help="dólares mínimos disponibles a ese precio (por defecto 5)")
    parser.add_argument("--monto", type=float, default=1.0, help="dólares por apuesta en papel (por defecto 1)")
    parser.add_argument("--comision", type=float, default=0.0, help="comisión de taker como fracción, p. ej. 0.01 = 1%% (por defecto 0)")
    parser.add_argument("--csv", type=Path, default=APUESTAS_CSV, help="archivo de apuestas (por defecto valor_apuestas.csv)")
    parser.add_argument("--max-filas", type=int, default=20)
    parser.add_argument("--liquidar", action="store_true", help="resolver las apuestas terminadas y mostrar el resultado")
    args = parser.parse_args()

    consola = Console()
    sesion = requests.Session()
    if args.liquidar:
        correr_liquidar(args, consola, sesion)
        return
    if not args.llave:
        parser.error("falta la llave de The Odds API: créala gratis en the-odds-api.com y usa --llave o ODDS_API_KEY")

    eventos: list[dict] = []
    mercados: list[MercadoPM] = []
    libros: dict[str, dict] = {}
    apuestas = leer_apuestas(args.csv)
    restantes = "?"
    error = None
    ultimo_momios = ultimo_precios = ultimo_mercados = -1e9

    def actualizar() -> Group:
        nonlocal eventos, mercados, libros, restantes, error, ultimo_momios, ultimo_precios, ultimo_mercados
        ahora = time.monotonic()
        try:
            if ahora - ultimo_mercados > 300:
                mercados = obtener_mercados_pm(sesion)
                ultimo_mercados = ahora
            # Cuando ya empezaron todos los partidos no hay apuestas nuevas: no se gastan créditos.
            por_empezar = any(m.inicio is None or m.inicio > datetime.now(timezone.utc) for m in mercados)
            if ahora - ultimo_momios > args.momios_cada and (por_empezar or not eventos):
                eventos, quedan = obtener_momios(sesion, args.llave, args.mercados, args.casa)
                restantes = quedan or restantes
                ultimo_momios = ahora
            if ahora - ultimo_precios > args.precios_cada:
                libros = obtener_libros(sesion, [m.token_id for m in mercados])
                ultimo_precios = ahora
            error = None
        except requests.RequestException as e:
            error = str(e)
        ops = comparar(mercados, eventos, libros, args.casa)
        momento = datetime.now(timezone.utc)
        nuevas = registrar(ops, apuestas, args.min_ventaja, args.min_disponible, args.monto, momento)
        apuestas.extend(nuevas)
        libro_de = {m.id: libros.get(m.token_id) for m in mercados}
        vendidas = revisar_ventas(apuestas, libro_de, momento, args.comision)
        if nuevas or vendidas:
            escribir_apuestas(args.csv, apuestas)
        estado = (f"momios hace {(ahora - ultimo_momios) / 60:.0f} min · créditos restantes: {restantes} · "
                  f"{datetime.now():%H:%M:%S}" + (f" · Error: {error}" if error else ""))
        return Group(tabla_oportunidades(ops, args.min_ventaja, args.casa, estado, args.max_filas),
                     tabla_apuestas(apuestas))

    try:
        with Live(actualizar(), console=consola, auto_refresh=False) as live:
            while True:
                time.sleep(5)
                live.update(actualizar(), refresh=True)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
