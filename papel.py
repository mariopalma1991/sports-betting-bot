"""Simulación "en papel" de la estrategia de scalping como maker.

Pone órdenes ficticias a los precios sugeridos del monitor y las da por llenas
solo cuando los datos reales lo justifican. No usa dinero ni toca tu wallet.

Reglas de llenado (aproximadas y conservadoras):
- Una compra ficticia a P se llena si alguien vende a P o menos (operación con
  lado SELL), o si aparece una venta en el libro a P o menos.
- Una venta ficticia a P se llena si alguien compra a P o más (lado BUY), o si
  la mejor compra del libro llega a P.
- Solo vemos las operaciones del primer resultado de cada mercado. Las que se
  cruzan contra el otro lado no cuentan, así que el simulador llena de menos.
- Supone que tu orden se llena completa; con montos chicos suele ser así.
"""

from __future__ import annotations

import csv
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from rich.console import Group
from rich.table import Table
from rich.text import Text

COMPRA_PENDIENTE = "compra pendiente"
ABIERTA = "esperando venta"


@dataclass
class Posicion:
    token_id: str
    partido: str
    tipo: str
    resultado: str
    estado: str
    precio_compra: float
    precio_venta: float
    stop: float
    acciones: float
    creada: float
    llenada: float | None = None


@dataclass
class Cerrada:
    partido: str
    tipo: str
    resultado: str
    precio_compra: float
    precio_salida: float
    acciones: float
    motivo: str        # "objetivo" o "stop"
    ganancia: float
    segundos: float
    cerrada: float


def _p(precio: float) -> str:
    """0.48 -> "0.48"; con ticks de 0.001, 0.975 -> "0.975"."""
    return f"{precio:.2f}" if abs(precio - round(precio, 2)) < 1e-9 else f"{precio:.3f}"


class SimuladorPapel:
    def __init__(self, presupuesto: float = 7.0, acciones: float = 5, stop: float = 0.03,
                 espera: float = 60, comision_taker: float = 0.0, csv_ruta: Path | None = None):
        self.presupuesto = presupuesto
        self.acciones = acciones
        self.stop = stop
        self.espera = espera
        self.comision_taker = comision_taker
        self.csv_ruta = csv_ruta
        self.posiciones: dict[str, Posicion] = {}
        self.cerradas: list[Cerrada] = []
        self.canceladas = 0
        self.inicio = time.time()
        self._precio_actual: dict[str, float] = {}

    # --- Lógica -------------------------------------------------------------

    def comprometido(self) -> float:
        return sum(p.precio_compra * p.acciones for p in self.posiciones.values())

    def paso(self, filas: dict, operaciones: list[dict], ahora: float | None = None) -> None:
        """Avanza la simulación con el estado actual del libro y las operaciones nuevas.

        filas: token_id -> Fila del monitor. operaciones: mensajes last_trade_price.
        """
        ahora = time.time() if ahora is None else ahora

        for op in operaciones:
            pos = self.posiciones.get(str(op.get("asset_id")))
            if pos is None:
                continue
            precio, lado = float(op["price"]), str(op.get("side", "")).upper()
            if pos.estado == COMPRA_PENDIENTE and lado == "SELL" and precio <= pos.precio_compra + 1e-9:
                self._llenar_compra(pos, ahora)
            elif pos.estado == ABIERTA and lado == "BUY" and precio >= pos.precio_venta - 1e-9:
                self._cerrar(pos, pos.precio_venta, "objetivo", ahora)

        for token, pos in list(self.posiciones.items()):
            f = filas.get(token)
            if f is None or f.compra is None or f.venta is None:
                continue
            self._precio_actual[token] = f.compra
            if pos.estado == COMPRA_PENDIENTE:
                if f.venta <= pos.precio_compra + 1e-9:
                    self._llenar_compra(pos, ahora)
                elif f.compra > pos.precio_compra + 1e-9 or ahora - pos.creada > self.espera:
                    # Alguien mejoró tu precio o pasó demasiado tiempo: se cancela y,
                    # si sigue habiendo oportunidad, se vuelve a poner al precio nuevo.
                    del self.posiciones[token]
                    self.canceladas += 1
            elif pos.estado == ABIERTA:
                if f.compra >= pos.precio_venta - 1e-9:
                    self._cerrar(pos, pos.precio_venta, "objetivo", ahora)
                elif f.compra <= pos.stop + 1e-9:
                    # El stop vende de inmediato a la mejor compra (como taker).
                    self._cerrar(pos, f.compra, "stop", ahora, taker=True)

        for token, f in filas.items():
            if token in self.posiciones or not f.oportunidad or f.compra_sugerida is None:
                continue
            if self.comprometido() + f.compra_sugerida * self.acciones > self.presupuesto + 1e-9:
                continue
            self.posiciones[token] = Posicion(
                token_id=token, partido=f.partido, tipo=f.tipo, resultado=f.resultado,
                estado=COMPRA_PENDIENTE, precio_compra=f.compra_sugerida,
                precio_venta=f.venta_sugerida, stop=round(f.compra_sugerida - self.stop, 4),
                acciones=self.acciones, creada=ahora,
            )

    def _llenar_compra(self, pos: Posicion, ahora: float) -> None:
        pos.estado = ABIERTA
        pos.llenada = ahora

    def _cerrar(self, pos: Posicion, precio: float, motivo: str, ahora: float, taker: bool = False) -> None:
        ganancia = (precio - pos.precio_compra) * pos.acciones
        if taker:
            ganancia -= precio * pos.acciones * self.comision_taker
        cerrada = Cerrada(
            partido=pos.partido, tipo=pos.tipo, resultado=pos.resultado,
            precio_compra=pos.precio_compra, precio_salida=precio, acciones=pos.acciones,
            motivo=motivo, ganancia=round(ganancia, 4),
            segundos=round(ahora - (pos.llenada or pos.creada), 1), cerrada=ahora,
        )
        self.cerradas.append(cerrada)
        del self.posiciones[pos.token_id]
        if self.csv_ruta:
            self._guardar(cerrada)

    def _guardar(self, cerrada: Cerrada) -> None:
        nueva = not self.csv_ruta.exists()
        with self.csv_ruta.open("a", newline="", encoding="utf-8") as archivo:
            escritor = csv.DictWriter(archivo, fieldnames=list(Cerrada.__dataclass_fields__))
            if nueva:
                escritor.writeheader()
            fila = asdict(cerrada)
            fila["cerrada"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cerrada.cerrada))
            escritor.writerow(fila)

    # --- Resumen ------------------------------------------------------------

    def resumen(self) -> dict:
        objetivos = [c for c in self.cerradas if c.motivo == "objetivo"]
        stops = [c for c in self.cerradas if c.motivo == "stop"]
        sin_realizar = sum((self._precio_actual.get(p.token_id, p.precio_compra) - p.precio_compra) * p.acciones
                           for p in self.posiciones.values() if p.estado == ABIERTA)
        return {
            "realizado": sum(c.ganancia for c in self.cerradas),
            "sin_realizar": sin_realizar,
            "objetivos": len(objetivos),
            "stops": len(stops),
            "ganado_objetivos": sum(c.ganancia for c in objetivos),
            "perdido_stops": sum(c.ganancia for c in stops),
            "canceladas": self.canceladas,
            "exito": len(objetivos) / len(self.cerradas) if self.cerradas else None,
            "minutos": (time.time() - self.inicio) / 60,
        }

    def panel(self, max_cerradas: int = 6) -> Group:
        r = self.resumen()
        def signo(v: float) -> Text:
            return Text(f"{'+' if v >= 0 else '-'}${abs(v):.2f}", style="green" if v > 0 else "red" if v < 0 else "")
        exito = "—" if r["exito"] is None else f"{r['exito']:.0%}"
        encabezado = Text.assemble(
            ("SIMULACIÓN EN PAPEL", "bold magenta"), f"  ({r['minutos']:.0f} min, sin dinero real)   ",
            "Ganancia: ", signo(r["realizado"]), "   abierta: ", signo(r["sin_realizar"]),
            f"   Vueltas completas: {r['objetivos']}   Stops: {r['stops']}   Éxito: {exito}",
            f"   Canceladas: {r['canceladas']}   Comprometido: ${self.comprometido():.2f} de ${self.presupuesto:.2f}",
        )

        abiertas = Table(title="Órdenes ficticias", title_justify="left", expand=True, header_style="bold")
        for col in ("Estado", "Partido", "Mercado", "Compra", "Venta obj.", "Stop", "Actual", "Resultado"):
            abiertas.add_column(col, justify="right" if col not in ("Estado", "Partido", "Mercado") else "left")
        for p in self.posiciones.values():
            actual = self._precio_actual.get(p.token_id)
            flotante = None if actual is None or p.estado != ABIERTA else (actual - p.precio_compra) * p.acciones
            abiertas.add_row(
                Text(p.estado, style="yellow" if p.estado == COMPRA_PENDIENTE else "cyan"),
                p.partido, p.tipo, _p(p.precio_compra), _p(p.precio_venta), _p(p.stop),
                "—" if actual is None else _p(actual), "—" if flotante is None else signo(flotante),
            )
        if not self.posiciones:
            abiertas.add_row("—", "esperando oportunidades", "", "", "", "", "", "")

        ultimas = Table(title="Últimas cerradas", title_justify="left", expand=True, header_style="bold")
        for col in ("Hora", "Partido", "Mercado", "Compra", "Salida", "Motivo", "Duración", "Resultado"):
            ultimas.add_column(col, justify="right" if col not in ("Partido", "Mercado", "Motivo") else "left")
        for c in reversed(self.cerradas[-max_cerradas:]):
            ultimas.add_row(
                time.strftime("%H:%M:%S", time.localtime(c.cerrada)), c.partido, c.tipo,
                _p(c.precio_compra), _p(c.precio_salida),
                Text(c.motivo, style="green" if c.motivo == "objetivo" else "red"),
                f"{c.segundos:.0f}s", signo(c.ganancia),
            )
        return Group(encabezado, abiertas, ultimas)
