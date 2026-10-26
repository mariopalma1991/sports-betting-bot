"""Interfaz en el navegador para la estrategia de recompensas de liquidez.

Junta en una página lo que hacen recompensas.py y ordenes.py:
    - Candidatos: mercados de deportes con premio que pasan los criterios (se actualiza cada 10 min).
    - Poner orden: formulario con las mismas revisiones que ordenes.py (solo deportes, tope de USD,
      post-only, si cuenta para el premio) y confirmación antes de mandar.
    - Mis órdenes: si cada una cuenta para el premio, tu parte de la bolsa, cuánto te daría al
      día, cuándo vence, cuándo cierra el mercado, alerta cuando el partido está por empezar y botones para cancelar.
    - Premio: estimado de hoy (para ver si pasas el mínimo de $1), cobrado ayer e historial.
    - Cartera: efectivo, valor del portafolio y tus posiciones (lo que sí compraste).

Corre un servidor pequeño en tu computadora (solo tú lo ves) y abre el navegador. Lee tus
llaves del archivo .env igual que ordenes.py; sin llaves solo muestra los candidatos.

Uso:
    python recompensas_ui.py               # abre http://localhost:8060
    python recompensas_ui.py --demo        # datos simulados, sin conectarse ni mandar nada
    python recompensas_ui.py --capital 25 --puerto 8060 --no-abrir
"""

from __future__ import annotations

import argparse
import json
import math
import re
import secrets
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import requests

import ordenes as od
from cuotas import obtener_libros
from nba_monitor import _fecha, _json_list
from recompensas import (
    analizar, datos_demo, etiquetas_deportes, historias_demo, motivo_descarte, obtener_historias, obtener_partidos,
    obtener_premios, precios_del_libro,
)

PAGINA = Path(__file__).with_name("recompensas_ui.html")
EVENTO_URL = "https://polymarket.com/market/"
ESCANEAR_CADA = 600       # segundos entre búsquedas de candidatos
CUENTA_CADA = 60          # segundos entre lecturas de tus órdenes
GANANCIAS_CADA = 600
DIAS_HISTORIAL = 7
MINIMO_PAGO = 1.0         # Polymarket no paga premios diarios menores a $1
MIN_SEGUNDOS_GTD = 4 * 60  # la API pide al menos 3 minutos; se deja margen
HOSTS_LOCALES = ("localhost", "127.0.0.1")
CANCELAR_ANTES = timedelta(hours=od.MIN_HORAS_ANTES)
PROXIMO_PARTIDO_CADA = 3600   # segundos entre búsquedas del próximo partido de un equipo
# "Will West Ham United be promoted..." -> "West Ham United"
EQUIPO_DE_PREGUNTA = re.compile(r"^Will (?:the )?(.+?) (?:be|win|finish|make|get|reach|qualify|avoid|go|score|advance|lift|claim)\b",
                                re.IGNORECASE)


def _f(v) -> float | None:
    return None if v is None else float(v)


def precio_sugerido(mejor_compra: float | None, mejor_venta: float | None, tick: float) -> float | None:
    """1¢ abajo del precio medio, o igualar al mejor comprador si queda más cerca; sin cruzar el libro."""
    if mejor_compra is None or mejor_venta is None:
        return None
    medio = (mejor_compra + mejor_venta) / 2
    sugerido = max(mejor_compra, math.floor(round((medio - 0.01) / tick, 6)) * tick)
    if sugerido >= mejor_venta:
        sugerido = mejor_venta - tick
    return round(sugerido, 4) if sugerido > 0 else None


def alerta_de_inicio(inicio: datetime | None, ahora: datetime, horas: float = od.MIN_HORAS_ANTES) -> str:
    if inicio is None:
        return ""
    faltan = (inicio - ahora).total_seconds() / 3600
    if faltan <= 0:
        return "El partido ya empezó: cancela"
    if faltan < horas:
        return f"Empieza en {faltan * 60:.0f} min: cancela"
    return ""


def bolsa_activa_de_configs(configs, ahora: datetime) -> float:
    """Suma la bolsa diaria de las configuraciones de premio vigentes de un mercado."""
    total = 0.0
    for c in configs:
        inicio, fin = getattr(c, "start_date", None), getattr(c, "end_date", None)
        if (inicio is None or inicio <= ahora) and (fin is None or fin > ahora):
            total += float(c.rate_per_day)
    return total


def repartir_estimados(ordenes: list[dict], porcentajes: dict[str, float], bolsas: dict[str, float | None]) -> float:
    """Pone en cada orden lo que te daría al día y regresa el total.

    Polymarket da tu parte por mercado; si tienes varias órdenes que cuentan en el mismo mercado,
    se reparte entre ellas según las acciones pendientes.
    """
    por_mercado: dict[str, list[dict]] = {}
    for o in ordenes:
        o["estimado_dia"] = None
        if o["cuenta"]:
            por_mercado.setdefault(o["condition_id"], []).append(o)
    total = 0.0
    for cid, grupo in por_mercado.items():
        bolsa, parte = bolsas.get(cid), porcentajes.get(cid)
        if bolsa is None or parte is None:
            continue
        mercado = parte / 100 * bolsa
        total += mercado
        pendientes = sum(o["acciones"] - o["llenadas"] for o in grupo) or len(grupo)
        for o in grupo:
            o["estimado_dia"] = mercado * ((o["acciones"] - o["llenadas"]) or 1) / pendientes
    return total


def vencimiento_desde_texto(texto: str | None, ahora: datetime) -> int | None:
    """Convierte la fecha del formulario (ISO con zona) en segundos Unix; None = GTC."""
    if not texto:
        return None
    vence = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    if vence.tzinfo is None:
        raise ValueError("La fecha de vencimiento necesita zona horaria.")
    if (vence - ahora).total_seconds() < MIN_SEGUNDOS_GTD:
        raise ValueError("El vencimiento tiene que ser al menos 4 minutos después de ahora.")
    return int(vence.timestamp())


def equipo_de_pregunta(pregunta: str | None) -> str | None:
    m = EQUIPO_DE_PREGUNTA.match((pregunta or "").strip())
    return m.group(1).strip() if m else None


def nombra_al_equipo(titulo: str, equipo: str) -> bool:
    """¿El título del partido menciona al equipo? Prueba el nombre completo y luego las dos primeras palabras."""
    titulo, palabras = titulo.lower(), equipo.lower().split()
    return equipo.lower() in titulo or (len(palabras) >= 2 and " ".join(palabras[:2]) in titulo)


def inicio_de_evento(e: dict) -> datetime | None:
    inicio = _fecha(e.get("startTime"))
    if inicio is None:
        inicio = next((_fecha(m.get("gameStartTime")) for m in e.get("markets") or [] if m.get("gameStartTime")), None)
    return inicio


def proximo_partido(eventos: list[dict], equipo: str, ahora: datetime) -> tuple[datetime, str] | None:
    """El primer partido por empezar del equipo entre los eventos de su liga."""
    partidos = []
    for e in eventos:
        titulo = e.get("title") or ""
        inicio = inicio_de_evento(e)
        if inicio and inicio > ahora and " vs" in titulo.lower() and nombra_al_equipo(titulo, equipo):
            partidos.append((inicio, titulo))
    return min(partidos) if partidos else None


def recomendacion_cancelar(inicio_partido: datetime | None, partido: str | None, ahora: datetime) -> dict:
    """Cuándo cancelar: 1 hora antes del partido (del mercado o del próximo del equipo)."""
    if inicio_partido is None:
        return {"fecha": None, "motivo": "No encontré el próximo partido: revisa el calendario"}
    fecha = inicio_partido - CANCELAR_ANTES
    return {"fecha": fecha.isoformat(), "motivo": f"1 h antes de {partido}" if partido else "1 h antes del partido",
            "ya": fecha <= ahora}


def fila_a_json(f, capital: float) -> dict:
    sim = next((s for s in f.simulaciones if s), None)
    return {
        "condition_id": f.condition_id, "mercado": f.partido, "tipo": f.mercado,
        "inicio": f.inicio.isoformat() if f.inicio else None, "horas": f.horas,
        "cierre": f.cierre.isoformat() if f.cierre else None,
        # Para la minigráfica: el precio cada ~3 horas (56 puntos en 7 días) y cuánto se movió.
        "historia": f.historia[::max(1, len(f.historia) // 56)], "movimiento": f.movimiento,
        "bolsa": f.bolsa_diaria, "min_acciones": f.min_acciones, "costo_minimo": f.costo_minimo,
        "max_distancia": f.max_distancia, "medio": f.medio, "spread": f.spread, "competencia": f.competencia,
        "capital": capital,
        "estimado": None if sim is None else {"ganancia": sim.ganancia, "parte": sim.parte, "lados": sim.lados},
        # Lo que paga cada dólar: USD estimados (hasta el inicio, máximo un día) ÷ capital.
        "rendimiento": f.rendimiento if f.rendimiento >= 0 else None,
        "motivo": f.motivo,
    }


class ClienteDemo:
    """Imita al cliente de Polymarket para --demo: guarda órdenes en memoria."""

    def __init__(self):
        self.ordenes: list[SimpleNamespace] = []

    def place_limit_order(self, *, token_id, side, price, size, post_only, expiration=None):
        oid = "0xdemo" + secrets.token_hex(8)
        self.ordenes.append(SimpleNamespace(
            id=oid, condition_id=self._cid.get(token_id, "?"), asset_id=token_id, side=side,
            outcome=self._resultado.get(token_id, "?"), price=Decimal(price), original_size=Decimal(size),
            size_matched=Decimal(0),
            expires_at=datetime.fromtimestamp(expiration, timezone.utc) if expiration else None))
        return SimpleNamespace(ok=True, status="live", order_id=oid)

    def list_market_rewards(self, condition_id):
        bolsa = self._bolsas.get(condition_id, 0)
        config = SimpleNamespace(rate_per_day=Decimal(str(bolsa)), start_date=None, end_date=None)
        return [SimpleNamespace(items=(SimpleNamespace(rewards_config=(config,)),))]

    def get_balance_allowance(self, asset_type):
        usado = sum(o.price * o.original_size for o in self.ordenes)
        return SimpleNamespace(balance=int((Decimal("27.76") - usado) * 10**6) if asset_type == "COLLATERAL" else 0)

    def list_positions(self):
        return [SimpleNamespace(items=(SimpleNamespace(
            condition_id="f2", asset_id="tf2-b", title="¿Bolton asciende a la Premier?", outcome="No",
            current_size=Decimal(10), avg_price=Decimal("0.80"), current_value=Decimal("8.50"),
            current_price=Decimal("0.85"), total_pnl=Decimal("0.50"), percent_pnl=Decimal("6.25"), slug="bolton"),))]

    def get_portfolio_value(self):
        return SimpleNamespace(value=Decimal("8.50"))

    def list_open_orders(self):
        return [SimpleNamespace(items=tuple(self.ordenes))]

    def get_orders_scoring(self, order_ids):
        """En el demo cuenta solo si llega al mínimo de 50 acciones del premio."""
        tamano = {o.id: o.original_size - o.size_matched for o in self.ordenes}
        return {oid: tamano.get(oid, 0) >= 50 for oid in order_ids}

    def get_reward_percentages(self):
        return {o.condition_id: 42.0 for o in self.ordenes}

    def get_total_earnings_for_user_for_day(self, date, sponsored=None):
        return (SimpleNamespace(earnings=Decimal(str(round(0.3 + (hash(date) % 90) / 100, 2)))),)

    def _cancelar(self, ids):
        antes = {o.id for o in self.ordenes}
        self.ordenes = [o for o in self.ordenes if o.id not in ids]
        return SimpleNamespace(canceled=tuple(i for i in ids if i in antes), not_canceled={})

    def cancel_orders(self, order_ids):
        return self._cancelar(set(order_ids))

    def cancel_all(self):
        return self._cancelar({o.id for o in self.ordenes})

    _cid: dict[str, str] = {}
    _resultado: dict[str, str] = {}
    _bolsas: dict[str, float] = {}


class Tablero:
    def __init__(self, demo: bool, capital: float, env: dict[str, str]):
        self.demo = demo
        self.capital = capital
        self.env = env
        self.max_usd = float(env.get("MAX_USD_POR_ORDEN") or od.MAX_USD_DEFECTO)
        self.sesion = requests.Session()
        self.candado = threading.Lock()
        self.ids_deportes: set[str] = set()
        self.mercados: dict[str, dict] = {}       # condition_id -> mercado de Gamma (para el formulario)
        self.libros: dict[str, dict] = {}         # token_id -> libro (solo demo)
        self.filas: list = []
        self.total_premios = 0
        self.escaneado: datetime | None = None
        self.escaneando = False
        self.error_escaneo: str | None = None
        self.cliente = ClienteDemo() if demo else None
        self.error_cuenta: str | None = None
        self.cuenta: dict = {"ordenes": [], "porcentajes": {}}
        self.ganancias: dict | None = None      # {"fecha", "total"} de ayer
        self.historial: list[dict] = []         # [{"fecha", "total"}] últimos días, del más reciente
        self._ganancias_hora = 0.0
        self.bolsas: dict[str, float | None] = {}  # condition_id -> bolsa diaria (de cualquier mercado)
        self._bolsas_hora: dict[str, float] = {}
        self.estimado_dia = 0.0                 # USD al día con tus órdenes y tu parte de ahora
        self.acumulado = {"fecha": None, "total": 0.0, "desde": None}  # estimado sumado mientras corre
        self._ultima_cuenta: float | None = None
        self.cartera: dict = {"efectivo": None, "posiciones_valor": None, "posiciones": []}
        self._inicios: dict[str, datetime | None] = {}
        self._proximos: dict[str, tuple[float, tuple[datetime, str] | None]] = {}  # cid -> (hora, partido)

    @property
    def tiene_llaves(self) -> bool:
        return self.demo or bool(self.env.get("SIGNER_PRIVATE_KEY"))

    # --- Candidatos ---
    def escanear(self) -> None:
        self.escaneando = True
        ahora = datetime.now(timezone.utc)
        try:
            if self.demo:
                partidos, premios, libros = datos_demo(ahora)
                self.ids_deportes = {"demo"}
                self.libros = libros
                mercados = {p.condition_id: self._mercado_demo(p, premios[p.condition_id]) for p in partidos}
                historias = historias_demo(partidos, libros)
            else:
                premios = obtener_premios(self.sesion, ahora)
                self.ids_deportes = etiquetas_deportes(self.sesion)
                partidos = obtener_partidos(self.sesion, list(premios), ahora, self.ids_deportes)
                libros = obtener_libros(self.sesion, [p.token_id for p in partidos])
                historias = obtener_historias(self.sesion, [p.token_id for p in partidos], ahora)
                mercados = {}
            filas = analizar(partidos, premios, libros, [self.capital], 1.0, ahora, historias)
            for f in filas:
                f.motivo = motivo_descarte(f)
            with self.candado:
                self.filas = filas
                self.total_premios = len(premios)
                self.mercados.update(mercados)
                self.bolsas.update({cid: pr.bolsa_diaria for cid, pr in premios.items()})
                self._inicios.update({p.condition_id: p.inicio for p in partidos})
                self.escaneado = ahora
                self.error_escaneo = None
        except requests.RequestException as e:
            self.error_escaneo = f"No pude leer Polymarket: {e}"
        finally:
            self.escaneando = False

    def _mercado_demo(self, p, premio) -> dict:
        ClienteDemo._bolsas[p.condition_id] = premio.bolsa_diaria
        ClienteDemo._cid.update({t: p.condition_id for t in [p.token_id, p.token_id + "-b"]})
        ClienteDemo._resultado.update({p.token_id: p.resultados[0], p.token_id + "-b": p.resultados[-1]})
        b = self.libros.get(p.token_id) or {}
        espejo = {"bids": [{"price": f"{1 - float(n['price']):.4f}", "size": n["size"]} for n in b.get("asks", [])],
                  "asks": [{"price": f"{1 - float(n['price']):.4f}", "size": n["size"]} for n in b.get("bids", [])],
                  "tick_size": b.get("tick_size", "0.01"), "min_order_size": "5"}
        self.libros[p.token_id + "-b"] = espejo
        self.libros[p.token_id] = {**b, "min_order_size": "5"}
        return {"question": p.partido if p.mercado == "Temporada" else f"{p.partido} · {p.mercado}",
                "conditionId": p.condition_id, "outcomes": json.dumps([p.resultados[0], p.resultados[-1]]),
                "clobTokenIds": json.dumps([p.token_id, p.token_id + "-b"]),
                "rewardsMinSize": premio.min_acciones, "rewardsMaxSpread": premio.max_distancia,
                "gameStartTime": p.inicio.isoformat() if p.inicio else None,
                "endDate": p.cierre.isoformat() if p.cierre else None,
                "events": [{"tags": [{"id": "demo"}]}], "acceptingOrders": True}

    # --- Formulario ---
    def mercado(self, texto: str) -> dict:
        with self.candado:
            m = self.mercados.get(texto)
        if m is None:
            m = od.buscar_mercado(self.sesion, texto)
            with self.candado:
                self.mercados[str(m.get("conditionId"))] = m
        return m

    def libro(self, token_id: str) -> dict:
        return self.libros[token_id] if self.demo else od.leer_libro(self.sesion, token_id)

    def ficha(self, texto: str) -> dict:
        """Lo que necesita el formulario: lados con su libro y un precio sugerido."""
        m = self.mercado(texto)
        lados = []
        for resultado, token in zip(_json_list(m.get("outcomes")), _json_list(m.get("clobTokenIds"))):
            libro = self.libro(str(token))
            compras, ventas = precios_del_libro(libro)
            mc, mv = (compras[0][0] if compras else None), (ventas[0][0] if ventas else None)
            tick = float(libro.get("tick_size") or 0.01)
            lados.append({"resultado": str(resultado), "mejor_compra": mc, "mejor_venta": mv, "tick": tick,
                          "medio": (mc + mv) / 2 if mc is not None and mv is not None else None,
                          "sugerido": precio_sugerido(mc, mv, tick)})
        cid = str(m.get("conditionId"))
        ahora = datetime.now(timezone.utc)
        if cid not in self._inicios:
            self._inicios[cid] = _fecha(m.get("gameStartTime"))
        return {"condition_id": cid, "pregunta": m.get("question"), "cancelar": self._cancelar_antes(cid, ahora),
                "slug": m.get("slug"), "min_acciones": od._num(m.get("rewardsMinSize")),
                "max_distancia": od._num(m.get("rewardsMaxSpread")), "lados": lados, "max_usd": self.max_usd}

    def revisar(self, datos: dict) -> tuple[dict, od.Revision]:
        m = self.mercado(str(datos["condition_id"]))
        resultado, token = od.token_del_lado(m, str(datos["lado"]))
        precio, acciones = float(datos["precio"]), float(datos["acciones"])
        rev = od.revisar(m, self.libro(token), resultado, token, precio, acciones, self.max_usd,
                         self.ids_deportes or etiquetas_deportes(self.sesion), datetime.now(timezone.utc))
        return m, rev

    def poner(self, datos: dict) -> dict:
        _, rev = self.revisar(datos)
        if rev.bloqueos:
            return {"ok": False, "revision": revision_a_json(rev)}
        vence = vencimiento_desde_texto(datos.get("vence"), datetime.now(timezone.utc))
        extra = {} if vence is None else {"expiration": vence}
        resp = self._cliente().place_limit_order(token_id=rev.token_id, side="BUY", price=str(rev.precio),
                                                 size=str(rev.acciones), post_only=True, **extra)
        if not resp.ok:
            return {"ok": False, "error": f"Polymarket rechazó la orden: {resp.message} ({resp.code})"}
        self.leer_cuenta()
        return {"ok": True, "order_id": resp.order_id, "estado": resp.status}

    # --- Cuenta ---
    def _cliente(self):
        if self.cliente is None:
            self.cliente = od.conectar(self.env)
        return self.cliente

    def _inicio_de(self, condition_id: str) -> datetime | None:
        if condition_id not in self._inicios:
            try:
                m = self.mercado(condition_id)
                self._inicios[condition_id] = _fecha(m.get("gameStartTime"))
            except (requests.RequestException, od.ErrorOrden):
                return None
        return self._inicios[condition_id]

    def _bolsa_de(self, cliente, condition_id: str, ahora: datetime) -> float | None:
        """Bolsa diaria de un mercado: la del último escaneo, o se pregunta a Polymarket (cada 10 min)."""
        if condition_id in self.bolsas and time.monotonic() - self._bolsas_hora.get(condition_id, 0) < ESCANEAR_CADA:
            return self.bolsas[condition_id]
        try:
            configs = [c for pagina in cliente.list_market_rewards(condition_id=condition_id)
                       for r in pagina.items for c in r.rewards_config]
            self.bolsas[condition_id] = bolsa_activa_de_configs(configs, ahora)
            self._bolsas_hora[condition_id] = time.monotonic()
        except Exception:  # sin dato de bolsa no hay estimado, pero lo demás sigue
            self.bolsas.setdefault(condition_id, None)
        return self.bolsas.get(condition_id)

    def _acumular(self, ahora: datetime) -> None:
        """Suma el estimado del tiempo transcurrido; se reinicia a medianoche UTC, cuando Polymarket paga."""
        hoy = ahora.strftime("%Y-%m-%d")
        if self.acumulado["fecha"] != hoy:
            self.acumulado = {"fecha": hoy, "total": 0.0, "desde": ahora.isoformat()}
            self._ultima_cuenta = None
        if self._ultima_cuenta is not None:
            segundos = min(time.monotonic() - self._ultima_cuenta, 2 * CUENTA_CADA)  # sin saltos si la PC se durmió
            self.acumulado["total"] += self.estimado_dia * segundos / 86400
        self._ultima_cuenta = time.monotonic()

    def leer_cartera(self, cliente) -> None:
        try:
            efectivo = cliente.get_balance_allowance(asset_type="COLLATERAL").balance / 10**6
        except Exception:
            efectivo = None
        posiciones = []
        try:
            for p in od._todas(cliente.list_positions()):
                if float(p.current_size) <= 0:
                    continue
                posiciones.append({
                    "mercado": p.title or self._pregunta(str(p.condition_id)), "lado": getattr(p, "outcome", None),
                    "acciones": float(p.current_size), "precio_promedio": float(p.avg_price),
                    "precio_actual": float(p.current_price), "valor": float(p.current_value),
                    "ganancia": float(p.total_pnl), "ganancia_pct": float(p.percent_pnl), "slug": p.slug,
                })
        except Exception as e:
            self.error_cuenta = f"No pude leer tus posiciones: {type(e).__name__}: {e}"
        self.cartera = {"efectivo": efectivo, "posiciones": posiciones,
                        "posiciones_valor": sum(p["valor"] for p in posiciones)}

    def leer_historial(self, cliente, ahora: datetime) -> None:
        historial = []
        for d in range(1, DIAS_HISTORIAL + 1):
            fecha = (ahora - timedelta(days=d)).strftime("%Y-%m-%d")
            totales = cliente.get_total_earnings_for_user_for_day(date=fecha, sponsored=True)
            historial.append({"fecha": fecha, "total": float(sum((Decimal(t.earnings) for t in totales), Decimal(0)))})
        self.historial = historial
        self.ganancias = historial[0]

    def _proximo_partido_de(self, condition_id: str, ahora: datetime) -> tuple[datetime, str] | None:
        """Para mercados de temporada: busca el próximo partido del equipo en los eventos de su liga."""
        guardado = self._proximos.get(condition_id)
        if guardado and time.monotonic() - guardado[0] < PROXIMO_PARTIDO_CADA:
            return guardado[1]
        resultado = None
        try:
            m = self.mercado(condition_id)
            equipo = equipo_de_pregunta(m.get("question"))
            etiquetas = [str(t.get("id")) for e in m.get("events") or [] for t in e.get("tags") or []
                         if str(t.get("id")) in self.ids_deportes and t.get("slug") != "sports"]
            if equipo and not self.demo:
                eventos = []
                for tag in etiquetas[:3]:
                    eventos += od._gamma(self.sesion, "events", {
                        "tag_id": tag, "closed": "false", "limit": 200, "order": "startDate", "ascending": "true",
                        "end_date_min": ahora.strftime("%Y-%m-%dT%H:%M:%SZ")})
                resultado = proximo_partido(eventos, equipo, ahora)
            elif self.demo and "West Ham" in (m.get("question") or ""):
                resultado = (ahora + timedelta(hours=30), "West Ham vs. Leeds")
        except (requests.RequestException, od.ErrorOrden):
            resultado = None
        self._proximos[condition_id] = (time.monotonic(), resultado)
        return resultado

    def _motivo_no_cuenta(self, condition_id: str, orden) -> str:
        """Por qué una orden no cuenta: la revisa contra el libro de ahora con las reglas del premio."""
        try:
            m = self.mercado(condition_id)
            resultado, token = od.token_del_lado(m, str(orden.outcome))
            restantes = float(orden.original_size - orden.size_matched)
            rev = od.revisar(m, self.libro(token), resultado, token, float(orden.price), restantes, float("inf"),
                             self.ids_deportes, datetime.now(timezone.utc))
        except (requests.RequestException, od.ErrorOrden):
            return "No pude revisar el libro"
        motivos = [a.removeprefix("No cuenta: ") for a in rev.avisos if a.startswith("No cuenta")]
        if rev.min_acciones and restantes < rev.min_acciones:
            motivos.append(f"Para que cuente: cancela y pon {rev.min_acciones:g} acciones "
                           f"(${rev.min_acciones * float(orden.price):.2f}).")
        return " ".join(motivos) or "Con el libro de ahora sí cumple: Polymarket puede tardar unos minutos en contarla."

    def _cancelar_antes(self, condition_id: str, ahora: datetime) -> dict:
        inicio = self._inicio_de(condition_id)
        if inicio is not None:
            return recomendacion_cancelar(inicio, None, ahora)
        proximo = self._proximo_partido_de(condition_id, ahora)
        return recomendacion_cancelar(*(proximo or (None, None)), ahora)

    def leer_cuenta(self) -> None:
        if not self.tiene_llaves:
            return
        try:
            cliente = self._cliente()
            ordenes = od._todas(cliente.list_open_orders())
            puntuan = cliente.get_orders_scoring(order_ids=[o.id for o in ordenes]) if ordenes else {}
            porcentajes = cliente.get_reward_percentages() or {}
            ahora = datetime.now(timezone.utc)
            filas = []
            for o in ordenes:
                cid = str(o.condition_id)
                m = self.mercados.get(cid) or {}
                vence = getattr(o, "expires_at", None)
                filas.append({
                    "id": o.id, "condition_id": cid, "mercado": m.get("question") or self._pregunta(cid),
                    "lado": f"{o.side} {o.outcome}", "precio": float(o.price),
                    "acciones": float(o.original_size), "llenadas": float(o.size_matched),
                    "costo": float(o.price * (o.original_size - o.size_matched)),
                    "cuenta": bool(puntuan.get(o.id)), "parte": _f(porcentajes.get(cid)),
                    "motivo": None if puntuan.get(o.id) else self._motivo_no_cuenta(cid, o),
                    "alerta": alerta_de_inicio(self._inicio_de(cid), ahora),
                    "vence": vence.isoformat() if vence else None,
                    "cierre": self._cierre_de(cid),
                })
            porcentajes = {str(k): float(v) for k, v in porcentajes.items()}
            bolsas = {cid: self._bolsa_de(cliente, cid, ahora) for cid in {o["condition_id"] for o in filas if o["cuenta"]}}
            for o in filas:
                o["bolsa"] = bolsas.get(o["condition_id"])
            self.error_cuenta = None
            self._acumular(ahora)
            self.estimado_dia = repartir_estimados(filas, porcentajes, bolsas)
            if time.monotonic() - self._ganancias_hora > GANANCIAS_CADA or self.ganancias is None:
                self.leer_historial(cliente, ahora)
                self._ganancias_hora = time.monotonic()
            self.leer_cartera(cliente)
            with self.candado:
                self.cuenta = {"ordenes": filas, "porcentajes": porcentajes}
        except od.ErrorOrden as e:
            self.error_cuenta = str(e)
        except Exception as e:  # errores de la librería o de red: se muestran en la página
            self.error_cuenta = f"{type(e).__name__}: {e}"

    def _cierre_de(self, cid: str) -> str | None:
        """Fecha en que cierra el mercado (endDate de Gamma)."""
        try:
            fecha = _fecha(self.mercado(cid).get("endDate"))
        except (requests.RequestException, od.ErrorOrden):
            return None
        return fecha.isoformat() if fecha else None

    def _pregunta(self, cid: str) -> str:
        try:
            return self.mercado(cid).get("question") or cid[:10]
        except (requests.RequestException, od.ErrorOrden):
            return cid[:10]

    def cancelar(self, ids: list[str] | None) -> dict:
        cliente = self._cliente()
        resp = cliente.cancel_all() if ids is None else cliente.cancel_orders(order_ids=ids)
        self.leer_cuenta()
        return {"canceladas": list(resp.canceled), "no_canceladas": dict(resp.not_canceled)}

    # --- Hilos y fotos ---
    def iniciar(self) -> None:
        threading.Thread(target=self._bucle_escaneo, daemon=True).start()
        threading.Thread(target=self._bucle_cuenta, daemon=True).start()

    def _bucle_escaneo(self) -> None:
        while True:
            self.escanear()
            time.sleep(ESCANEAR_CADA)

    def _bucle_cuenta(self) -> None:
        while True:
            self.leer_cuenta()
            time.sleep(CUENTA_CADA)

    def foto_candidatos(self) -> dict:
        with self.candado:
            filas = list(self.filas)
        return {"demo": self.demo, "escaneando": self.escaneando, "error": self.error_escaneo,
                "escaneado": self.escaneado.isoformat() if self.escaneado else None,
                "total_premios": self.total_premios, "deportes": len(filas),
                "filas": [fila_a_json(f, self.capital) for f in filas]}

    def foto_cuenta(self) -> dict:
        with self.candado:
            cuenta = dict(self.cuenta)
        ahora = datetime.now(timezone.utc)
        fin_del_dia = (ahora + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        restante = self.estimado_dia * (fin_del_dia - ahora).total_seconds() / 86400
        return {"llaves": self.tiene_llaves, "error": self.error_cuenta, "max_usd": self.max_usd,
                "ganancias": self.ganancias, "historial": self.historial, "minimo_pago": MINIMO_PAGO,
                "estimado": {"dia": self.estimado_dia, "acumulado": self.acumulado["total"],
                             "desde": self.acumulado["desde"], "cierre": fin_del_dia.isoformat(),
                             "al_cierre": self.acumulado["total"] + restante},
                "cartera": self.cartera, **cuenta}


def revision_a_json(rev: od.Revision) -> dict:
    return {"pregunta": rev.pregunta, "resultado": rev.resultado, "precio": rev.precio, "acciones": rev.acciones,
            "costo": rev.costo, "mejor_compra": rev.mejor_compra, "mejor_venta": rev.mejor_venta,
            "medio": rev.medio, "spread": rev.spread, "distancia": rev.distancia,
            "min_acciones": rev.min_acciones, "max_distancia": rev.max_distancia,
            "cuenta": rev.cuenta_para_premio, "bloqueos": rev.bloqueos, "avisos": rev.avisos}


def manejador(tablero: Tablero, ficha_secreta: str) -> type[BaseHTTPRequestHandler]:
    class Manejador(BaseHTTPRequestHandler):
        def _local(self) -> bool:
            # Evita que una página de internet use este servidor (DNS rebinding).
            return (self.headers.get("Host") or "").rsplit(":", 1)[0] in HOSTS_LOCALES

        def do_GET(self):
            if not self._local():
                return self.send_error(403)
            if self.path == "/":
                html = PAGINA.read_text(encoding="utf-8").replace("__FICHA__", ficha_secreta)
                self._responder(html.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/candidatos":
                self._json(tablero.foto_candidatos())
            elif self.path == "/api/cuenta":
                self._json(tablero.foto_cuenta())
            else:
                self.send_error(404)

        def do_POST(self):
            # Solo la página que sirvió este servidor conoce la ficha: otra página no puede mandar órdenes.
            if not self._local() or not secrets.compare_digest(self.headers.get("X-Ficha", ""), ficha_secreta):
                return self.send_error(403)
            try:
                datos = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/api/ficha":
                    self._json(tablero.ficha(str(datos["mercado"])))
                elif self.path == "/api/revisar":
                    self._json(revision_a_json(tablero.revisar(datos)[1]))
                elif self.path == "/api/poner":
                    self._json(tablero.poner(datos))
                elif self.path == "/api/cancelar":
                    self._json(tablero.cancelar([str(i) for i in datos["ids"]]))
                elif self.path == "/api/cancelar-todo":
                    self._json(tablero.cancelar(None))
                elif self.path == "/api/escanear":
                    if not tablero.escaneando:
                        threading.Thread(target=tablero.escanear, daemon=True).start()
                    self._json({"ok": True})
                else:
                    self.send_error(404)
            except (KeyError, ValueError, od.ErrorOrden) as e:
                self._json({"error": str(e)}, 400)
            except requests.RequestException as e:
                self._json({"error": f"No pude conectarme con Polymarket: {e}"}, 502)
            except Exception as e:  # errores de la librería de Polymarket
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def _json(self, datos: dict, codigo: int = 200) -> None:
            self._responder(json.dumps(datos).encode(), "application/json", codigo)

        def _responder(self, cuerpo: bytes, tipo: str, codigo: int = 200) -> None:
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(cuerpo)

        def log_message(self, *args):
            pass

    return Manejador


def main() -> None:
    parser = argparse.ArgumentParser(description="Interfaz de recompensas de liquidez de Polymarket")
    parser.add_argument("--demo", action="store_true", help="datos simulados; no se conecta ni manda órdenes")
    parser.add_argument("--capital", type=float, default=25, help="capital para estimar tu parte (por defecto 25 USD)")
    parser.add_argument("--puerto", type=int, default=8060, help="puerto local (por defecto 8060)")
    parser.add_argument("--no-abrir", action="store_true", help="no abrir el navegador solo")
    args = parser.parse_args()

    tablero = Tablero(args.demo, args.capital, {} if args.demo else od.cargar_env())
    print("Modo demo: datos simulados." if args.demo else
          "Buscando candidatos en Polymarket (la primera vez tarda un par de minutos)…")
    if not tablero.tiene_llaves:
        print(f"Sin llaves en {od.ARCHIVO_ENV}: solo se verán los candidatos.")
    tablero.iniciar()
    # 127.0.0.1: solo tu computadora puede abrir la página.
    servidor = ThreadingHTTPServer(("127.0.0.1", args.puerto), manejador(tablero, secrets.token_urlsafe(24)))
    url = f"http://localhost:{args.puerto}"
    print(f"Interfaz en {url}  (Ctrl+C para salir)")
    if not args.no_abrir:
        webbrowser.open(url)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
