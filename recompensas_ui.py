"""Interfaz en el navegador para la estrategia de recompensas de liquidez.

Junta en una página lo que hacen recompensas.py y ordenes.py:
    - Candidatos: mercados de deportes con premio que pasan los criterios (se actualiza cada 10 min).
    - Poner orden: formulario con las mismas revisiones que ordenes.py (solo deportes, tope de USD,
      post-only, si cuenta para el premio) y confirmación antes de mandar.
    - Mis órdenes: si cada una cuenta para el premio, tu parte de la bolsa, alerta cuando el
      partido está por empezar y botones para cancelar.
    - Premio cobrado ayer.

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
    analizar, datos_demo, etiquetas_deportes, motivo_descarte, obtener_partidos, obtener_premios,
    precios_del_libro,
)

PAGINA = Path(__file__).with_name("recompensas_ui.html")
EVENTO_URL = "https://polymarket.com/market/"
ESCANEAR_CADA = 600       # segundos entre búsquedas de candidatos
CUENTA_CADA = 60          # segundos entre lecturas de tus órdenes
GANANCIAS_CADA = 600
HOSTS_LOCALES = ("localhost", "127.0.0.1")


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


def fila_a_json(f, capital: float) -> dict:
    sim = next((s for s in f.simulaciones if s), None)
    return {
        "condition_id": f.condition_id, "mercado": f.partido, "tipo": f.mercado,
        "inicio": f.inicio.isoformat() if f.inicio else None, "horas": f.horas,
        "bolsa": f.bolsa_diaria, "min_acciones": f.min_acciones, "costo_minimo": f.costo_minimo,
        "max_distancia": f.max_distancia, "medio": f.medio, "spread": f.spread, "competencia": f.competencia,
        "capital": capital,
        "estimado": None if sim is None else {"ganancia": sim.ganancia, "parte": sim.parte, "lados": sim.lados},
        "motivo": f.motivo,
    }


class ClienteDemo:
    """Imita al cliente de Polymarket para --demo: guarda órdenes en memoria."""

    def __init__(self):
        self.ordenes: list[SimpleNamespace] = []

    def place_limit_order(self, *, token_id, side, price, size, post_only):
        oid = "0xdemo" + secrets.token_hex(8)
        self.ordenes.append(SimpleNamespace(
            id=oid, condition_id=self._cid.get(token_id, "?"), asset_id=token_id, side=side,
            outcome=self._resultado.get(token_id, "?"), price=Decimal(price), original_size=Decimal(size),
            size_matched=Decimal(0)))
        return SimpleNamespace(ok=True, status="live", order_id=oid)

    def list_open_orders(self):
        return [SimpleNamespace(items=tuple(self.ordenes))]

    def get_orders_scoring(self, order_ids):
        return {oid: True for oid in order_ids}

    def get_reward_percentages(self):
        return {o.condition_id: 42.0 for o in self.ordenes}

    def get_total_earnings_for_user_for_day(self, date, sponsored=None):
        return (SimpleNamespace(earnings=Decimal("1.25")),)

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
        self.ganancias: dict | None = None
        self._ganancias_hora = 0.0
        self._inicios: dict[str, datetime | None] = {}

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
            else:
                premios = obtener_premios(self.sesion, ahora)
                self.ids_deportes = etiquetas_deportes(self.sesion)
                partidos = obtener_partidos(self.sesion, list(premios), ahora, self.ids_deportes)
                libros = obtener_libros(self.sesion, [p.token_id for p in partidos])
                mercados = {}
            filas = analizar(partidos, premios, libros, [self.capital], 1.0, ahora)
            for f in filas:
                f.motivo = motivo_descarte(f)
            with self.candado:
                self.filas = filas
                self.total_premios = len(premios)
                self.mercados.update(mercados)
                self._inicios.update({p.condition_id: p.inicio for p in partidos})
                self.escaneado = ahora
                self.error_escaneo = None
        except requests.RequestException as e:
            self.error_escaneo = f"No pude leer Polymarket: {e}"
        finally:
            self.escaneando = False

    def _mercado_demo(self, p, premio) -> dict:
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
        return {"condition_id": str(m.get("conditionId")), "pregunta": m.get("question"),
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
        resp = self._cliente().place_limit_order(token_id=rev.token_id, side="BUY", price=str(rev.precio),
                                                 size=str(rev.acciones), post_only=True)
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
                filas.append({
                    "id": o.id, "condition_id": cid, "mercado": m.get("question") or self._pregunta(cid),
                    "lado": f"{o.side} {o.outcome}", "precio": float(o.price),
                    "acciones": float(o.original_size), "llenadas": float(o.size_matched),
                    "costo": float(o.price * (o.original_size - o.size_matched)),
                    "cuenta": bool(puntuan.get(o.id)), "parte": _f(porcentajes.get(cid)),
                    "alerta": alerta_de_inicio(self._inicio_de(cid), ahora),
                })
            if time.monotonic() - self._ganancias_hora > GANANCIAS_CADA or self.ganancias is None:
                fecha = (ahora - timedelta(days=1)).strftime("%Y-%m-%d")
                totales = cliente.get_total_earnings_for_user_for_day(date=fecha, sponsored=True)
                self.ganancias = {"fecha": fecha, "total": float(sum((Decimal(t.earnings) for t in totales), Decimal(0)))}
                self._ganancias_hora = time.monotonic()
            with self.candado:
                self.cuenta = {"ordenes": filas, "porcentajes": {k: float(v) for k, v in porcentajes.items()}}
                self.error_cuenta = None
        except od.ErrorOrden as e:
            self.error_cuenta = str(e)
        except Exception as e:  # errores de la librería o de red: se muestran en la página
            self.error_cuenta = f"{type(e).__name__}: {e}"

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
        return {"llaves": self.tiene_llaves, "error": self.error_cuenta, "max_usd": self.max_usd,
                "ganancias": self.ganancias, **cuenta}


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
