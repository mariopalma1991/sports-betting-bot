import marcador_nba as mn

JUEGO = {
    "gameId": "0022600031", "gameStatus": 2, "gameStatusText": "Q3 4:12", "period": 3,
    "gameClock": "PT04M12.00S", "gameTimeUTC": "2026-10-26T02:00:00Z", "regulationPeriods": 4,
    "homeTeam": {"teamName": "Jazz", "teamTricode": "UTA", "score": 77,
                 "periods": [{"period": 1, "score": 25}, {"period": 2, "score": 30}, {"period": 3, "score": 22},
                             {"period": 4, "score": 0}]},
    "awayTeam": {"teamName": "Lakers", "teamTricode": "LAL", "score": 83,
                 "periods": [{"period": 1, "score": 28}, {"period": 2, "score": 31}, {"period": 3, "score": 24},
                             {"period": 4, "score": 0}]},
    "gameLeaders": {"homeLeaders": {"name": "Lauri Markkanen", "teamTricode": "UTA", "points": 20, "rebounds": 6, "assists": 2},
                    "awayLeaders": {"name": "LeBron James", "teamTricode": "LAL", "points": 24, "rebounds": 8, "assists": 6}},
}


def test_reloj_y_cuartos():
    assert mn.reloj("PT04M12.00S") == "4:12"
    assert mn.reloj("PT00M38.40S") == "0:38"
    assert mn.reloj("") == ""
    assert [mn.nombre_cuarto(p) for p in (1, 4, 5)] == ["Q1", "Q4", "OT"]


def test_resumir_en_juego():
    m = mn.resumir(JUEGO)
    assert (m["estado"], m["etiqueta"], m["cuartos"]) == ("en juego", "Q3 · 4:12", 4)
    assert (m["visitante"]["siglas"], m["visitante"]["puntos"], m["local"]["cuartos"]) == ("LAL", 83, [25, 30, 22, 0])
    assert [l["nombre"] for l in m["lideres"]] == ["LeBron James", "Lauri Markkanen"]


def test_resumir_medio_tiempo_y_final():
    medio = mn.resumir({**JUEGO, "period": 2, "gameClock": "", "gameStatusText": "Half"})
    assert medio["etiqueta"] == "Half"
    final = mn.resumir({**JUEGO, "gameStatus": 3, "period": 5})
    assert (final["estado"], final["etiqueta"], final["cuartos"]) == ("final", "Final (OT)", 5)
    assert mn.resumir({**JUEGO, "gameStatus": 1, "period": 0})["estado"] == "por empezar"


def test_emparejar_por_nombre_o_siglas():
    juegos = [mn.resumir(JUEGO)]
    assert mn.emparejar("Lakers vs. Jazz", "nba-lal-uta-2026-10-25", juegos)["id"] == "0022600031"
    # nombre distinto pero siglas iguales
    assert mn.emparejar("LA Lakers vs. Utah", "nba-lal-uta-2026-10-25", juegos) is not None
    assert mn.emparejar("Heat vs. Knicks", "nba-mia-nyk-2026-10-25", juegos) is None
