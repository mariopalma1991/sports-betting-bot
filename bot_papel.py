"""El bot en papel del dashboard: dos estrategias con dinero ficticio y saldo en vivo.

- Spread (estrategia B de papel.py): compra 1 tick arriba de la mejor compra y vende
  1 tick abajo de la mejor venta, solo antes del partido y con precio entre 0.20 y 0.80.
- Comparador (vender_justo de cuotas.py): compra en Polymarket cuando está más barato
  que la probabilidad justa de Pinnacle y deja una venta a ese precio justo. Si la venta
  no se llena, se queda hasta el final del partido. Necesita la llave de The Odds API.

Cada estrategia empieza con su presupuesto y el saldo arrastra lo ganado y lo perdido.
Las operaciones se guardan en bot_spread.csv y bot_comparador.csv, así que el saldo
sigue donde se quedó la próxima vez que abras el dashboard.
"""

from __future__ import annotations

import csv
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from cuotas import (
    Apuesta, comparar, escribir_apuestas, leer_apuestas, liquidar, obtener_libros, obtener_mercado,
    obtener_mercados_pm, obtener_momios, precio_de_venta, registrar, revisar_ventas,
)
from papel import ABIERTA, ESTRATEGIAS, Cerrada, SimuladorPapel

PRESUPUESTO = 7.0
SPREAD_CSV = Path("bot_spread.csv")
COMPARADOR_CSV = Path("bot_comparador.csv")
BUSCAR_MERCADOS_CADA = 300  # segundos
LIQUIDAR_CADA = 600         # segundos entre revisiones de partidos terminados
TERMINA_TRAS = timedelta(hours=2, minutes=30)  # antes de esto no vale la pena preguntar si ya se resolvió


def leer_cerradas(ruta: Path) -> list[Cerrada]:
    if not ruta.exists():
        return []
    with ruta.open(encoding="utf-8") as archivo:
        return [Cerrada(
            estrategia=f["estrategia"], partido=f["partido"], tipo=f["tipo"], resultado=f["resultado"],
            precio_compra=float(f["precio_compra"]), precio_salida=float(f["precio_salida"]),
            acciones=float(f["acciones"]), motivo=f["motivo"], ganancia=float(f["ganancia"]),
            segundos=float(f["segundos"]), cerrada=time.mktime(time.strptime(f["cerrada"], "%Y-%m-%d %H:%M:%S")),
        ) for f in csv.DictReader(archivo)]


def _ms(momento: str) -> float | None:
    """"2026-10-25 18:02:11" (hora local) -> milisegundos para la página."""
    try:
        return time.mktime(time.strptime(momento, "%Y-%m-%d %H:%M:%S")) * 1000
    except (TypeError, ValueError):
        return None


class BotSpread:
    """Estrategia B de papel.py. El presupuesto baja con las pérdidas: lo perdido ya no se usa."""

    def __init__(self, presupuesto: float = PRESUPUESTO, csv_ruta: Path | None = SPREAD_CSV):
        self.inicial = presupuesto
        self.sim = SimuladorPapel("B", ESTRATEGIAS["B"], presupuesto, csv_ruta=csv_ruta)
        self.sim.cerradas = leer_cerradas(csv_ruta) if csv_ruta else []
        self.candado = threading.Lock()

    def realizado(self) -> float:
        return sum(c.ganancia for c in self.sim.cerradas)

    def paso(self, filas: dict, operaciones: list[dict], ahora: float | None = None) -> None:
        with self.candado:
            self.sim.presupuesto = max(self.inicial + self.realizado(), 0.0)
            self.sim.paso(filas, operaciones, ahora)

    def foto(self) -> dict:
        with self.candado:
            r = self.sim.resumen()
            saldo = self.inicial + r["realizado"]
            comprometido = self.sim.comprometido()
            abiertas = []
            for p in self.sim.posiciones.values():
                actual = self.sim._precio_actual.get(p.token_id)
                abiertas.append({
                    "partido": p.partido, "mercado": p.tipo, "lado": p.resultado, "estado": p.estado,
                    "compra": p.precio_compra, "venta": p.precio_venta, "stop": p.stop, "actual": actual,
                    "flotante": None if actual is None or p.estado != ABIERTA
                    else round((actual - p.precio_compra) * p.acciones, 4),
                })
            cerradas = [{
                "hora": c.cerrada * 1000, "partido": c.partido, "mercado": c.tipo, "lado": c.resultado,
                "compra": c.precio_compra, "salida": c.precio_salida, "motivo": c.motivo, "ganancia": c.ganancia,
            } for c in reversed(self.sim.cerradas[-100:])]
        return {
            "nombre": "Spread (estrategia B)",
            "descripcion": "Compra 1 tick arriba de la mejor compra y vende 1 tick abajo de la mejor venta, "
                           f"{self.sim.acciones:g} acciones por orden, stop a {self.sim.stop * 100:.0f}¢. "
                           f"Filtros: {self.sim.filtros.describir()}.",
            "activo": True, "mensaje": None,
            "inicial": self.inicial, "saldo": saldo, "en_ordenes": comprometido, "libre": saldo - comprometido,
            "realizado": r["realizado"], "sin_realizar": r["sin_realizar"],
            "datos": f"{r['objetivos']} vendidas al objetivo · {r['stops']} stops · {r['canceladas']} compras canceladas",
            "abiertas": abiertas, "cerradas": cerradas,
        }


class BotComparador:
    """vender_justo de cuotas.py, solo con el mercado de ganador (1 crédito por consulta de momios)."""

    def __init__(self, llave: str | None, presupuesto: float = PRESUPUESTO, monto: float = 1.0,
                 min_ventaja: float = 0.01, min_disponible: float = 5, momios_cada: float = 1800,
                 casa: str = "pinnacle", csv_ruta: Path = COMPARADOR_CSV, demo: bool = False):
        self.llave = llave
        self.demo = demo
        self.inicial = presupuesto
        self.monto = monto
        self.min_ventaja = min_ventaja
        self.min_disponible = min_disponible
        self.momios_cada = momios_cada
        self.casa = casa
        self.csv_ruta = csv_ruta
        self.apuestas: list[Apuesta] = [a for a in leer_apuestas(csv_ruta) if a.estrategia == "vender_justo"]
        self.mercados = []
        self.eventos: list[dict] = []
        self.libros: dict[str, dict] = {}
        self.oportunidades = []
        self.creditos: str | None = None
        self.momios_en: datetime | None = None
        self.error: str | None = None
        self.candado = threading.Lock()
        self._ultimo_mercados = self._ultimo_momios = self._ultimo_liquidar = -1e9

    @property
    def activo(self) -> bool:
        return bool(self.llave) and not self.demo

    def abiertas(self) -> list[Apuesta]:
        return [a for a in self.apuestas if not a.resultado]

    def realizado(self) -> float:
        return sum(float(a.ganancia) for a in self.apuestas if a.resultado and a.ganancia)

    def libre(self) -> float:
        return self.inicial + self.realizado() - sum(a.monto for a in self.abiertas())

    def paso(self, sesion: requests.Session) -> None:
        """Pide precios (y momios cuando toca) y avanza las apuestas. Se llama cada ~30 s."""
        if not self.activo:
            return
        ahora, momento = time.monotonic(), datetime.now(timezone.utc)
        try:
            if ahora - self._ultimo_mercados > BUSCAR_MERCADOS_CADA:
                self.mercados = [m for m in obtener_mercados_pm(sesion) if m.tipo == "h2h"]
                self._ultimo_mercados = ahora
            # Solo se apuesta antes del partido: sin partidos por empezar no se gastan créditos.
            por_empezar = any(m.inicio is None or m.inicio > momento for m in self.mercados)
            if por_empezar and ahora - self._ultimo_momios > self.momios_cada:
                self.eventos, quedan = obtener_momios(sesion, self.llave, "h2h", self.casa)
                self.creditos = quedan or self.creditos
                self._ultimo_momios, self.momios_en = ahora, momento
            self.libros = obtener_libros(sesion, [m.token_id for m in self.mercados])
            if ahora - self._ultimo_liquidar > LIQUIDAR_CADA:
                self._ultimo_liquidar = ahora
                self._liquidar(sesion, momento)
            self.error = None
        except requests.RequestException as e:
            self.error = str(e)
        self.evaluar(momento)

    def evaluar(self, momento: datetime) -> None:
        with self.candado:
            self.oportunidades = comparar(self.mercados, self.eventos, self.libros, self.casa)
            nuevas: list[Apuesta] = []
            for a in registrar(self.oportunidades, self.apuestas, self.min_ventaja, self.min_disponible,
                               self.monto, momento):
                if a.estrategia == "vender_justo" and self.libre() - len(nuevas) * self.monto >= a.monto - 1e-9:
                    nuevas.append(a)
            self.apuestas.extend(nuevas)
            libro_de = {m.id: self.libros.get(m.token_id) for m in self.mercados}
            if revisar_ventas(self.apuestas, libro_de, momento) or nuevas:
                escribir_apuestas(self.csv_ruta, self.apuestas)

    def _liquidar(self, sesion: requests.Session, momento: datetime) -> None:
        terminados = {a.mercado_id for a in self.abiertas()
                      if a.inicio and datetime.fromisoformat(a.inicio) + TERMINA_TRAS < momento}
        cerrados = {mid: m for mid in terminados if (m := obtener_mercado(sesion, mid))}
        with self.candado:
            if liquidar(self.apuestas, cerrados):
                escribir_apuestas(self.csv_ruta, self.apuestas)

    def foto(self) -> dict:
        base = {
            "nombre": "Comparador (vender al precio justo)",
            "descripcion": f"Compra ${self.monto:g} antes del partido cuando Polymarket está al menos "
                           f"{self.min_ventaja * 100:g}¢ más barato que la probabilidad justa de {self.casa.title()}, "
                           "y deja una venta a ese precio justo. Si no se vende, espera al final del partido.",
            "activo": self.activo,
        }
        if not self.activo:
            base["mensaje"] = ("En modo demo no hay momios reales: esta estrategia solo corre con datos reales."
                               if self.demo else "Falta la llave de The Odds API. Cierra el dashboard y ábrelo así: "
                               "export ODDS_API_KEY=tu_llave  y luego  python dashboard.py")
        with self.candado:
            libro_de = {m.id: self.libros.get(m.token_id) for m in self.mercados}
            abiertas, sin_realizar = [], 0.0
            for a in self.abiertas():
                actual = precio_de_venta(libro_de.get(a.mercado_id), a.indice)
                flotante = None if actual is None else round(a.monto / a.precio * actual - a.monto, 4)
                sin_realizar += flotante or 0
                abiertas.append({
                    "partido": a.partido, "mercado": a.titulo, "lado": a.lado,
                    "estado": "esperando venta", "compra": a.precio, "venta": float(a.venta) if a.venta else None,
                    "stop": None, "actual": actual, "flotante": flotante,
                })
            cerradas = []
            for a in reversed([a for a in self.apuestas if a.resultado][-100:]):
                salida = float(a.venta) if a.resultado == "vendida" else 1.0 if a.resultado == "ganada" else 0.0
                cerradas.append({
                    "hora": _ms(a.vendida or a.momento), "partido": a.partido, "mercado": a.titulo,
                    "lado": a.lado, "compra": a.precio, "salida": salida, "motivo": a.resultado,
                    "ganancia": float(a.ganancia or 0),
                })
            mejores = [{"partido": o.partido, "lado": o.lado, "precio": o.precio, "justa": round(o.prob_justa, 4),
                        "ventaja": o.ventaja} for o in self.oportunidades[:5] if o.ventaja is not None]
            realizado = self.realizado()
            en_ordenes = sum(a.monto for a in self.abiertas())
        momios = (f"momios hace {(datetime.now(timezone.utc) - self.momios_en).total_seconds() / 60:.0f} min"
                  if self.momios_en else "sin momios todavía")
        return {
            **base, "mensaje": base.get("mensaje") or self.error,
            "inicial": self.inicial, "saldo": self.inicial + realizado, "en_ordenes": en_ordenes,
            "libre": self.inicial + realizado - en_ordenes, "realizado": realizado, "sin_realizar": sin_realizar,
            "datos": f"{momios} · créditos restantes: {self.creditos or '?'}",
            "abiertas": abiertas, "cerradas": cerradas, "mejores": mejores,
        }
