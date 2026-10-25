import dashboard as db
import nba_monitor as nm


EVENTO = {
    "title": "Lakers vs. Jazz",
    "slug": "nba-lal-uta-2026-10-25",
    "markets": [{
        "question": "Lakers vs. Jazz", "sportsMarketType": "moneyline",
        "outcomes": '["Lakers", "Jazz"]', "clobTokenIds": '["111", "222"]',
        "gameStartTime": "2026-10-26 02:00:00+00", "volume24hr": 20000,
    }, {
        "question": "Spread", "groupItemTitle": "Hándicap -6.5",
        "outcomes": '["Lakers", "Jazz"]', "clobTokenIds": '["333", "444"]',
        "gameStartTime": "2026-10-26 02:00:00+00",
    }],
}


def test_partido_de_evento_y_widget():
    p = db.partido_de_evento(EVENTO)
    assert (p.slug, p.titulo, p.inicio.hour, len(p.mercados)) == ("nba-lal-uta-2026-10-25", "Lakers vs. Jazz", 2, 2)
    assert db.url_widget(p.slug) == ("https://embed.polymarket.com/sports?market=nba-lal-uta-2026-10-25"
                                     "&theme=dark&creator=Marhum&height=300")


def test_a_json_usa_los_libros():
    p = db.partido_de_evento(EVENTO)
    datos = db.a_json(p, {"111": nm.Libro(bids={0.76: 1000}, asks={0.78: 1000})})
    ganador, handicap = datos["mercados"]
    assert (ganador["compra"], ganador["venta"], ganador["spread"], ganador["con_libro"]) == (0.76, 0.78, 0.02, True)
    assert handicap["con_libro"] is False
    assert datos["enlace"] == "https://polymarket.com/event/nba-lal-uta-2026-10-25"


def test_demo_agrupa_por_partido():
    fuente = nm.FuenteDemo()
    partidos = db.partidos_demo(fuente)
    assert len(partidos) == len(nm.FuenteDemo.PARTIDOS)
    assert all(len(p.mercados) == 3 for p in partidos)
    assert db.a_json(partidos[0], fuente.copia(), demo=True)["widget"] is None


def test_margen_exacto():
    assert db.es_margen_exacto({"title": "Pacers vs. Nets - Exact Margin", "slug": "nba-ind-bkn-2026-10-25-x"})
    assert not db.es_margen_exacto(EVENTO)
