"""Cadeiras: o framework escolhendo cerebro por vaga, e dividindo a partida.

    python tests/test_cadeiras.py

Uma CADEIRA e a vaga na partida, nao quem a ocupa. O framework so precisa saber
qual funil joga cada cadeira, e — quando ha mais de um driver — qual cadeira e
a dele. Estes testes cobrem exatamente essas duas coisas.

Nao encostam na chave de API nem na sessao do usuario: tudo que sai para fora e
substituido por dublê.
"""
import json
import threading
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "framework"))

import funnel                     # noqa: E402
import framework_server as fs     # noqa: E402

# A sessao do usuario nao e cobaia: qualquer `lembrar()` daqui morre aqui.
fs._save_session = lambda dados: None

passou = 0


def teste(nome):
    def envolve(fn):
        global passou
        fn()
        passou += 1
        print(f"OK {nome}")
    return envolve


def funil(fid, *, seat="", game="", nome=None):
    """Um funil minimo, valido, que emite uma acao so."""
    return funnel.sanitize_funnel({
        "id": fid, "name": nome or fid, "game": game, "seat": seat,
        "entry": "no", "nodes": {"no": {
            "label": "no",
            "questions": {"q": {"type": "noul", "label": "q",
                                "instructions": "instrucao", "threshold": 0.5}},
            "outputs": {"q": {"sim": {"to": None, "emit": "acao"},
                              "nao": {"to": None, "emit": None}}},
        }},
    })


# ---------------------------------------------------------------- sanitize

@teste("o sanitize deixa a cadeira passar")
def _():
    f = funil("x", seat="azul_1")
    assert f["seat"] == "azul_1", f"a cadeira sumiu no sanitize: {f.get('seat')!r}"


@teste("funil sem cadeira continua valendo: o campo e opcional")
def _():
    f = funil("x", game="street_football")
    assert f["seat"] == "", f"cadeira inventada do nada: {f['seat']!r}"


@teste("a cadeira sobrevive a ida e volta do disco")
def _():
    f = funil("x", seat="laranja_1")
    devolta = funnel.sanitize_funnel(json.loads(json.dumps(f)))
    assert devolta["seat"] == "laranja_1"


# ------------------------------------------------------------ _pick_funnel

class DriverFalso:
    """So a parte do Driver que escolhe o cerebro."""
    _pick_funnel = fs.Driver._pick_funnel

    def __init__(self, funnel_id="auto"):
        self.funnel_id = funnel_id


def com_funis(mapa):
    original = funnel.load_funnels
    funnel.load_funnels = lambda: mapa
    return original


@teste("no automatico, a cadeira decide qual cerebro pensa")
def _():
    antes = com_funis({
        "azul": funil("azul", seat="azul_1"),
        "laranja": funil("laranja", seat="laranja_1"),
    })
    try:
        d = DriverFalso()
        assert d._pick_funnel("street_football", "azul_1")["id"] == "azul"
        assert d._pick_funnel("street_football", "laranja_1")["id"] == "laranja"
    finally:
        funnel.load_funnels = antes


@teste("sem cadeira no frame, cai no jogo, como sempre foi")
def _():
    antes = com_funis({"solo": funil("solo", game="street_football")})
    try:
        assert DriverFalso()._pick_funnel("street_football", None)["id"] == "solo"
        assert DriverFalso()._pick_funnel("street_football", "")["id"] == "solo"
    finally:
        funnel.load_funnels = antes


@teste("funil escolhido na mao vence a cadeira")
def _():
    antes = com_funis({
        "azul": funil("azul", seat="azul_1"),
        "meu": funil("meu", seat="laranja_1"),
    })
    try:
        d = DriverFalso(funnel_id="meu")
        assert d._pick_funnel("street_football", "azul_1")["id"] == "meu", \
            "o funil fixado no editor tem que continuar mandando"
    finally:
        funnel.load_funnels = antes


@teste("cadeira sem dono da erro que diz o que fazer")
def _():
    antes = com_funis({"solo": funil("solo", game="street_football")})
    try:
        try:
            DriverFalso()._pick_funnel("street_football", "azul_1")
        except ValueError as e:
            assert "azul_1" in str(e), f"o erro nao diz qual cadeira: {e}"
            assert "funil" in str(e).lower(), f"o erro nao diz o que fazer: {e}"
        else:
            raise AssertionError("aceitou cadeira que nenhum funil declara")
    finally:
        funnel.load_funnels = antes


# ------------------------------------------------- driver: a cadeira e minha

class DriverDeMentira(fs.Driver):
    """Driver sem thread, sem rede e sem chave."""

    def __init__(self, seat="", frame=None):
        self.target = "http://127.0.0.1:0"
        self.funnel_id = "auto"
        self.seat = seat
        self.hz = 1.0
        self.running = False
        # `busy` agora e derivado: uma cadeira ocupada de cada vez
        self.ocupadas = {}
        self.cadeiras = []
        self._trava = threading.Lock()
        self.last = self.error = None
        self.frames = 0
        self._frame = frame or {}
        self.mandadas = []

    def _get_frame(self):
        return self._frame

    def _send_action(self, action, frame_no, signals, seat=""):
        self.mandadas.append({"action": action, "seat": seat})
        return {"ok": True}


@teste("driver com cadeira ignora o frame que nao e dele")
def _():
    d = DriverDeMentira(seat="laranja_1", frame={"seat": "azul_1", "game": "street_football",
                                                 "actions": [{"id": "acao"}], "state": {}})
    assert d.step() is None
    assert d.mandadas == [], "jogou no lugar do outro"
    assert d.error is None, f"tratou vez alheia como erro: {d.error}"
    assert d.frames == 0


@teste("driver sem cadeira joga todas: e o caso de um PC so")
def _():
    antes = com_funis({"azul": funil("azul", seat="azul_1")})
    chamou = []
    original = funnel.run_funnel
    funnel.run_funnel = lambda *a, **k: ("acao", [], 1.0, {}, {})
    fs._get_api_key = lambda: "chave-de-mentira"
    try:
        d = DriverDeMentira(seat="", frame={"seat": "azul_1", "game": "street_football",
                                            "actions": [{"id": "acao"}], "state": {}})
        d.step()
        assert d.mandadas == [{"action": "acao", "seat": "azul_1"}], \
            f"nao jogou a cadeira oferecida: {d.mandadas}"
    finally:
        funnel.load_funnels = antes
        funnel.run_funnel = original


@teste("a acao vai enderecada, e o log diz qual cadeira jogou")
def _():
    antes = com_funis({"azul": funil("azul", seat="azul_1")})
    original = funnel.run_funnel
    funnel.run_funnel = lambda *a, **k: ("acao", [], 1.0, {}, {})
    fs._get_api_key = lambda: "chave-de-mentira"
    try:
        d = DriverDeMentira(seat="azul_1", frame={"seat": "azul_1", "game": "street_football",
                                                  "actions": [{"id": "acao"}], "state": {}})
        r = d.step()
        assert d.mandadas[0]["seat"] == "azul_1", "a acao saiu sem cadeira no corpo"
        assert r["seat"] == "azul_1"
        assert r["agent"] == "azul_1", \
            f"o log rotulou por {r['agent']!r}: numa partida de varios Jevs isso fica ilegivel"
    finally:
        funnel.load_funnels = antes
        funnel.run_funnel = original


@teste("o corpo que sai na rede leva a cadeira")
def _():
    capturado = {}

    class RespostaFalsa:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok":true}'

    def urlopen_falso(req, timeout=None):
        capturado["body"] = json.loads(req.data.decode("utf-8"))
        return RespostaFalsa()

    original = fs.urlopen
    fs.urlopen = urlopen_falso
    try:
        d = DriverDeMentira()
        fs.Driver._send_action(d, "chutar", 7, {}, "laranja_1")
        assert capturado["body"]["seat"] == "laranja_1", \
            f"a cadeira nao saiu no corpo: {capturado['body']}"
        assert capturado["body"]["action"] == "chutar"
    finally:
        fs.urlopen = original


@teste("a sessao lembra a cadeira junto com o resto")
def _():
    guardado = {}
    original = fs._save_session
    fs._save_session = lambda dados: guardado.update(dados)
    try:
        d = DriverDeMentira(seat="laranja_1")
        fs.Driver.lembrar(d)
        assert guardado.get("seat") == "laranja_1", f"a cadeira nao foi guardada: {guardado}"
        assert "target" in guardado and "funnel" in guardado, "a sessao perdeu o resto"
    finally:
        fs._save_session = original


# ------------------------------------------------------- os funis do duelo

@teste("os dois funis do duelo declaram cadeiras diferentes")
def _():
    todos = funnel.load_funnels()
    azul, laranja = todos.get("racha_azul"), todos.get("racha_laranja")
    assert azul and laranja, "os funis do duelo sumiram"
    assert azul["seat"] == "azul_1"
    assert laranja["seat"] == "laranja_1"
    assert not funnel.validate_funnel(azul), funnel.validate_funnel(azul)
    assert not funnel.validate_funnel(laranja), funnel.validate_funnel(laranja)
    assert azul["seat"] != laranja["seat"], "dois cerebros na mesma cadeira"


@teste("os dois funis do duelo tem formatos diferentes de verdade")
def _():
    todos = funnel.load_funnels()
    azul, laranja = todos["racha_azul"], todos["racha_laranja"]
    assert len(azul["nodes"]) == 1, "o azul devia ser de um no so"
    assert len(laranja["nodes"]) > 1, \
        "o laranja devia ramificar: dois grafos iguais nao mostram nada no editor"




@teste("o driver aprende o elenco pelo frame")
def _():
    d = DriverDeMentira(frame={"seat": "azul_1", "seats": ["azul_1", "laranja_1"],
                               "game": "street_football", "actions": [{"id": "acao"}], "state": {}})
    d._pick_funnel = lambda game_id, seat=None: {"id": "f", "entry": "n", "nodes": {}}
    try:
        d.step()
    except Exception:
        pass   # sem chave de API o pensamento falha; o elenco ja foi lido antes disso
    assert d.cadeiras == ["azul_1", "laranja_1"], f"nao leu o elenco do frame: {d.cadeiras}"


@teste("um pensador por cadeira, nao um para o driver inteiro")
def _():
    d = DriverDeMentira()
    d.cadeiras = ["azul_1", "laranja_1"]
    assert d._pensadores() == ["azul_1", "laranja_1"]
    # uma cadeira pensando nao pode impedir a outra de pensar
    d.ocupadas["azul_1"] = True
    assert d.busy is True
    assert d.ocupadas.get("laranja_1") is None, "a outra cadeira ficou presa junto"


@teste("driver de uma cadeira so ignora o resto do elenco")
def _():
    d = DriverDeMentira(seat="laranja_1")
    d.cadeiras = ["azul_1", "laranja_1"]
    assert d._pensadores() == ["laranja_1"], "driver de uma maquina quis jogar tudo"


@teste("sem elenco anunciado, segue com um pensador anonimo")
def _():
    d = DriverDeMentira()
    assert d._pensadores() == [""], "jogo de uma cadeira so deixou de ser pilotavel"


@teste("409 do juiz nao derruba a partida")
def _():
    from urllib.error import HTTPError

    d = DriverDeMentira(frame={"seat": "azul_1", "seats": ["azul_1"],
                               "game": "street_football", "actions": [{"id": "acao"}], "state": {}})
    d.running = True

    def recusa(*a, **k):
        raise HTTPError("http://x/api/action", 409, "Nao e a vez", {}, None)

    d._send_action = recusa
    d._pick_funnel = lambda game_id, seat=None: {"id": "f", "entry": "n", "nodes": {}}
    fs.funnel.run_funnel = lambda *a, **k: ("acao", [], 1.0, {}, {})
    # a cadeira vem do laco, como acontece de verdade
    assert d.step('azul_1') is None
    assert d.running is True, "uma corrida entre pensadores parou a partida inteira"
    assert d.error is None, f"tratou 409 como erro: {d.error}"
    assert d.ocupadas.get("azul_1") is False, "a cadeira ficou presa depois do 409"




@teste("os funis do racha contam a regra da area e o corpo do adversario")
def _():
    funis = funnel.load_funnels()
    for fid in ("racha_azul", "racha_laranja"):
        texto = json.dumps(funis[fid], ensure_ascii=False).lower()
        assert "na_area_do_gol" in texto or "pequena area" in texto, (
            f"{fid} nao fala da area: o Jev chutaria de longe e todo gol seria anulado")
        assert "encostando" in texto, (
            f"{fid} nao usa 'encostando': o Jev insiste em atravessar o adversario")
        assert "campo de ataque" not in texto, (
            f"{fid} ainda manda chutar do campo de ataque, que virou regra velha")


print()
print(f"{passou} testes de cadeira passaram.")
