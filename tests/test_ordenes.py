from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

import ordenes as od

AHORA = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
DEPORTES = {"82"}
MERCADO = {
    "question": "Will West Ham be promoted?", "conditionId": "0xc1", "slug": "west-ham-promoted",
    "outcomes": '["Yes", "No"]', "clobTokenIds": '["tyes", "tno"]',
    "rewardsMinSize": 50, "rewardsMaxSpread": 4, "acceptingOrders": True,
    "events": [{"title": "EFL Championship", "tags": [{"id": "82", "slug": "efl"}]}],
}
LIBRO_NO = {"bids": [{"price": "0.21", "size": "100"}], "asks": [{"price": "0.23", "size": "100"}],
            "tick_size": "0.01", "min_order_size": "5"}


def revisar(precio=0.21, acciones=50, mercado=MERCADO, libro=LIBRO_NO, max_usd=15):
    return od.revisar(mercado, libro, "No", "tno", precio, acciones, max_usd, DEPORTES, AHORA)


def test_cargar_env(tmp_path, monkeypatch):
    ruta = tmp_path / ".env"
    ruta.write_text("# comentario\nSIGNER_PRIVATE_KEY=0xabc\nPOLYMARKET_WALLET_ADDRESS = \"0xdef\"\n\nMAX_USD_POR_ORDEN=15\n",
                    encoding="utf-8")
    monkeypatch.delenv("SIGNER_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("MAX_USD_POR_ORDEN", "20")
    env = od.cargar_env(ruta)
    assert env == {"SIGNER_PRIVATE_KEY": "0xabc", "POLYMARKET_WALLET_ADDRESS": "0xdef", "MAX_USD_POR_ORDEN": "20"}
    assert od.cargar_env(tmp_path / "no-existe") == {}


def test_token_del_lado():
    assert od.token_del_lado(MERCADO, "no") == ("No", "tno")
    assert od.token_del_lado(MERCADO, "Sí") == ("Yes", "tyes")
    with pytest.raises(od.ErrorOrden):
        od.token_del_lado(MERCADO, "empate")


def test_revision_buena_cuenta_para_premio():
    rev = revisar()
    assert rev.bloqueos == [] and rev.avisos == []
    assert rev.cuenta_para_premio
    assert rev.medio == pytest.approx(0.22) and rev.distancia == pytest.approx(1.0)
    assert rev.costo == pytest.approx(10.5)


def test_revision_bloqueos():
    assert any("deportes" in b for b in revisar(mercado={**MERCADO, "events": [{"tags": [{"id": "1"}]}]}).bloqueos)
    assert any("tope" in b for b in revisar(acciones=100).bloqueos)            # 21 USD > 15
    assert any("al momento" in b for b in revisar(precio=0.23).bloqueos)       # cruza el libro
    assert any("pasos" in b for b in revisar(precio=0.215).bloqueos)
    assert any("mínimo" in b for b in revisar(acciones=2).bloqueos)
    assert any("no acepta" in b for b in revisar(mercado={**MERCADO, "closed": True}).bloqueos)


def test_revision_avisos_no_cuenta():
    lejos = revisar(precio=0.17)                                              # 5¢ del medio, máximo 4¢
    assert not lejos.cuenta_para_premio and not lejos.bloqueos
    assert not revisar(acciones=20).cuenta_para_premio                        # premio pide 50
    assert not revisar(mercado={**MERCADO, "rewardsMaxSpread": None}).cuenta_para_premio
    un_lado = revisar(libro={**LIBRO_NO, "asks": []})
    assert un_lado.medio is None and not un_lado.cuenta_para_premio
    pronto = {**MERCADO, "gameStartTime": (AHORA + timedelta(minutes=30)).isoformat()}
    assert any("1 hora" in a for a in revisar(mercado=pronto).avisos)


class Sesion:
    """Imita requests.Session para Gamma."""

    def __init__(self, respuestas):
        self.respuestas = respuestas
        self.pedidos = []

    def get(self, url, params=None, timeout=None):
        self.pedidos.append((url, params))
        clave = url.rsplit("/", 1)[-1], (params or {}).get("slug") or (params or {}).get("condition_ids")
        datos = self.respuestas.get(clave, [])
        return SimpleNamespace(json=lambda: datos, raise_for_status=lambda: None)


def test_buscar_mercado_por_link_y_evento():
    sesion = Sesion({("markets", "west-ham-promoted"): [MERCADO]})
    assert od.buscar_mercado(sesion, "https://polymarket.com/event/efl-promotion/west-ham-promoted") is MERCADO
    evento = {"title": "EFL", "tags": [{"id": "82"}],
              "markets": [{**MERCADO, "events": None, "slug": "a"}, {**MERCADO, "events": None, "slug": "b"}]}
    sesion = Sesion({("events", "efl-promotion"): [evento]})
    with pytest.raises(od.ErrorOrden, match="varios mercados"):
        od.buscar_mercado(sesion, "https://polymarket.com/event/efl-promotion")
    evento["markets"][1]["closed"] = True
    m = od.buscar_mercado(sesion, "efl-promotion")
    assert m["slug"] == "a" and m["events"] == [evento]


class Cliente:
    def __init__(self, ordenes=()):
        self.ordenes = list(ordenes)
        self.puestas = []
        self.canceladas = []

    def place_limit_order(self, **kw):
        self.puestas.append(kw)
        return SimpleNamespace(ok=True, status="live", order_id="0xorden1")

    def list_open_orders(self):
        return [SimpleNamespace(items=tuple(self.ordenes))]

    def get_orders_scoring(self, order_ids):
        return {oid: True for oid in order_ids}

    def cancel_orders(self, order_ids):
        self.canceladas.extend(order_ids)
        return SimpleNamespace(canceled=tuple(order_ids), not_canceled={})


def _args(**kw):
    base = dict(mercado="west-ham-promoted", lado="no", precio=0.21, acciones=50, prueba=False, si=True)
    return SimpleNamespace(**{**base, **kw})


def _preparar(monkeypatch):
    monkeypatch.setattr(od, "buscar_mercado", lambda s, t: MERCADO)
    monkeypatch.setattr(od, "leer_libro", lambda s, t: LIBRO_NO)
    monkeypatch.setattr(od, "etiquetas_deportes", lambda s: DEPORTES)


def test_poner_manda_post_only(monkeypatch):
    _preparar(monkeypatch)
    cliente = Cliente()
    od.cmd_poner(_args(), {"MAX_USD_POR_ORDEN": "15"}, None, cliente)
    assert cliente.puestas == [{"token_id": "tno", "side": "BUY", "price": "0.21", "size": "50", "post_only": True}]


def test_poner_prueba_y_bloqueo_no_mandan(monkeypatch):
    _preparar(monkeypatch)
    cliente = Cliente()
    od.cmd_poner(_args(prueba=True), {}, None, cliente)
    with pytest.raises(od.ErrorOrden):
        od.cmd_poner(_args(acciones=100), {"MAX_USD_POR_ORDEN": "15"}, None, cliente)
    assert cliente.puestas == []


def test_cancelar_por_prefijo(monkeypatch):
    monkeypatch.setattr(od, "preguntas_por_condicion", lambda s, c: {})
    orden = lambda oid: SimpleNamespace(id=oid, condition_id="0xc1", side="BUY", outcome="No", price=Decimal("0.21"),
                                        original_size=Decimal(50), size_matched=Decimal(0))
    cliente = Cliente([orden("0xabc111"), orden("0xabd222")])
    od.cmd_cancelar(SimpleNamespace(ids=["0xabc"]), {}, None, cliente)
    assert cliente.canceladas == ["0xabc111"]
    with pytest.raises(od.ErrorOrden, match="coincide con 2"):
        od.cmd_cancelar(SimpleNamespace(ids=["0xab"]), {}, None, cliente)
    od.cmd_ver(None, {}, None, cliente)   # no truena con Decimals
