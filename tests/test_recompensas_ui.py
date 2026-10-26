import json
import threading
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

import recompensas_ui as ui

AHORA = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def test_precio_sugerido():
    assert ui.precio_sugerido(0.21, 0.23, 0.01) == pytest.approx(0.21)    # medio-1¢ = 0.21, igual al mejor comprador
    assert ui.precio_sugerido(0.10, 0.20, 0.01) == pytest.approx(0.14)    # medio 0.15 → 0.14
    assert ui.precio_sugerido(0.500, 0.501, 0.001) == pytest.approx(0.5)  # nunca cruza el libro
    assert ui.precio_sugerido(None, 0.5, 0.01) is None


def test_alerta_de_inicio():
    assert ui.alerta_de_inicio(None, AHORA) == ""
    assert ui.alerta_de_inicio(AHORA + timedelta(hours=3), AHORA) == ""
    assert "30 min" in ui.alerta_de_inicio(AHORA + timedelta(minutes=30), AHORA)
    assert "ya empezó" in ui.alerta_de_inicio(AHORA - timedelta(minutes=1), AHORA)


@pytest.fixture
def tablero():
    t = ui.Tablero(demo=True, capital=25, env={})
    t.escanear()
    return t


def test_demo_poner_ver_y_cancelar(tablero):
    candidatos = [f for f in tablero.foto_candidatos()["filas"] if not f["motivo"]]
    assert [f["condition_id"] for f in candidatos] == ["f1"]
    ficha = tablero.ficha("f1")
    no = next(l for l in ficha["lados"] if l["resultado"] == "No")
    assert no["medio"] == pytest.approx(0.23) and no["sugerido"] == pytest.approx(0.22)
    # Bloqueada: pasa el tope de 15 USD.
    r = tablero.poner({"condition_id": "f1", "lado": "No", "precio": 0.22, "acciones": 100})
    assert not r["ok"] and r["revision"]["bloqueos"]
    assert tablero.foto_cuenta()["ordenes"] == []
    r = tablero.poner({"condition_id": "f1", "lado": "No", "precio": 0.22, "acciones": 50})
    assert r["ok"]
    ordenes = tablero.foto_cuenta()["ordenes"]
    assert len(ordenes) == 1 and ordenes[0]["cuenta"] and ordenes[0]["costo"] == pytest.approx(11)
    assert tablero.cancelar([ordenes[0]["id"]])["canceladas"] == [ordenes[0]["id"]]
    assert tablero.foto_cuenta()["ordenes"] == []


def test_servidor_pide_ficha_y_host_local(tablero):
    servidor = ThreadingHTTPServer(("127.0.0.1", 0), ui.manejador(tablero, "secreta"))
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    puerto = servidor.server_address[1]

    def pedir(metodo, ruta, cabeceras=None, cuerpo=None):
        c = HTTPConnection("127.0.0.1", puerto, timeout=5)
        c.request(metodo, ruta, body=cuerpo, headers=cabeceras or {})
        r = c.getresponse()
        return r.status, r.read()

    try:
        estado, html = pedir("GET", "/")
        assert estado == 200 and b"secreta" in html
        assert pedir("GET", "/", {"Host": "malo.com"})[0] == 403
        assert pedir("POST", "/api/cancelar-todo", cuerpo=b"{}")[0] == 403
        assert pedir("POST", "/api/cancelar-todo", {"X-Ficha": "otra"}, b"{}")[0] == 403
        estado, cuerpo = pedir("POST", "/api/ficha", {"X-Ficha": "secreta", "Content-Type": "application/json"},
                               json.dumps({"mercado": "f1"}).encode())
        assert estado == 200 and json.loads(cuerpo)["condition_id"] == "f1"
    finally:
        servidor.shutdown()
