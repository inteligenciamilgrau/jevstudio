#!/usr/bin/env python3
"""Sistema de jogos — servidor passivo.

O jogo fica aberto e rodando sozinho. Nao conhece o framework, nao conhece o
Jev e nao chama ninguem: quem quiser joga-lo pede um frame e manda uma acao,
a qualquer momento. Framework, curl, um bot em Godot ou o proprio navegador
sao todos apenas clientes.

    GET  /api/frame     -> estado atual + acoes validas agora
    POST /api/action    -> {"action": "<id>"} aplica e devolve o frame novo
    GET  /api/handshake -> quem sou eu e como falar comigo

    python game_server.py               # http://127.0.0.1:8100
"""
import json
import os
import math
import random
import sys
import threading
import time
from collections import deque
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
MAPS_FILE = ROOT / ".jev_maps.json"
SESSION_FILE = ROOT / ".jev_session.json"
SERVICE = "jev-games"
VERSION = "1.0"


# ==========================================================================
# Mapas do Vitamina
# Um mapa fixa o tabuleiro: onde ficam as paredes, de onde o player sai e em
# quais celulas a vitamina aparece. Com isso da para montar desafio repetivel,
# em vez do tabuleiro sorteado a cada reset.
# ==========================================================================

MAP_MIN, MAP_MAX = 4, 24


def _sanitize_map(raw):
    if not isinstance(raw, dict):
        return None
    try:
        cols = max(MAP_MIN, min(MAP_MAX, int(raw.get("cols", 12))))
        rows = max(MAP_MIN, min(MAP_MAX, int(raw.get("rows", 10))))
    except (TypeError, ValueError):
        cols, rows = 12, 10

    def celula(valor, padrao=(0, 0)):
        try:
            if isinstance(valor, dict):
                x, y = int(valor.get("x")), int(valor.get("y"))
            else:
                x, y = int(valor[0]), int(valor[1])
        except (TypeError, ValueError, IndexError, KeyError):
            return padrao
        return (max(0, min(cols - 1, x)), max(0, min(rows - 1, y)))

    def lista(bruto):
        vistas, fora = set(), []
        if isinstance(bruto, list):
            for item in bruto[:cols * rows]:
                c = celula(item, None)
                if c and c not in vistas:
                    vistas.add(c)
                    fora.append(list(c))
        return fora

    start = celula(raw.get("start"), (0, 0))
    walls = [w for w in lista(raw.get("walls")) if tuple(w) != start]
    spawns = [sp for sp in lista(raw.get("spawns"))
              if tuple(sp) != start and sp not in walls]
    nome = str(raw.get("name") or "").strip()[:60]
    fid = str(raw.get("id") or "").strip()[:60] or "mapa"
    return {"id": fid, "name": nome or fid, "cols": cols, "rows": rows,
            "start": list(start), "walls": walls, "spawns": spawns}


def _read_maps():
    try:
        dados = json.loads(MAPS_FILE.read_text(encoding="utf-8"))
        return dados if isinstance(dados, dict) else {}
    except (OSError, ValueError):
        return {}


def load_maps():
    saida = {}
    for mid, bruto in _read_maps().items():
        limpo = _sanitize_map(bruto)
        if limpo:
            saida[str(mid)[:60]] = limpo
    return saida


def save_map(raw):
    limpo = _sanitize_map(raw)
    if not limpo:
        raise ValueError("Mapa invalido: esperava um objeto JSON.")
    guardados = _read_maps()
    guardados[limpo["id"]] = limpo
    MAPS_FILE.write_text(json.dumps(guardados, indent=2, ensure_ascii=False), encoding="utf-8")
    return limpo


def delete_map(map_id):
    guardados = _read_maps()
    existia = guardados.pop(str(map_id), None) is not None
    MAPS_FILE.write_text(json.dumps(guardados, indent=2, ensure_ascii=False), encoding="utf-8")
    return existia


# ==========================================================================
# Jogos. Cada um: actions, reset, state, apply, view, stats.
# ==========================================================================

class VitaminGame:
    id, name = "vitamin_hunt", "Vitamina"
    funnel = "vitamina_eixo"
    actions = [{"id": "up", "label": "↑ Cima"}, {"id": "down", "label": "↓ Baixo"},
               {"id": "left", "label": "← Esquerda"}, {"id": "right", "label": "→ Direita"}]
    COLS, ROWS, BARRIERS = 12, 10, 8
    DIRS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}

    def __init__(self):
        self.map = None          # None = tabuleiro sorteado, como sempre foi
        self.reset()

    def load_map(self, mapa):
        """mapa=None volta ao sorteio."""
        self.map = mapa
        self.reset()

    def reset(self):
        self.score = self.steps = self.blocked = 0
        self._spawn_index = 0
        if self.map:
            self.COLS, self.ROWS = self.map["cols"], self.map["rows"]
            self.player = list(self.map["start"])
            self.barriers = {tuple(w) for w in self.map["walls"]}
            self.vitamin = self._next_spawn()
        else:
            self.COLS, self.ROWS = type(self).COLS, type(self).ROWS
            self.player = [self.COLS // 2, self.ROWS // 2]
            self._build_barriers()
            self.vitamin = self._free_cell()

    def _next_spawn(self):
        """Percorre os spawns na ordem: o desafio fica repetivel."""
        pontos = [tuple(sp) for sp in (self.map or {}).get("spawns", [])]
        alcancaveis = set(self._reachable())
        validos = [sp for sp in pontos if sp in alcancaveis]
        if not validos:
            return self._free_cell()
        escolhido = validos[self._spawn_index % len(validos)]
        self._spawn_index += 1
        return list(escolhido)

    def _inside(self, x, y):
        return 0 <= x < self.COLS and 0 <= y < self.ROWS

    def _build_barriers(self):
        cells = [(x, y) for y in range(self.ROWS) for x in range(self.COLS)
                 if [x, y] != self.player]
        for _ in range(60):
            picked = random.sample(cells, self.BARRIERS)
            self.barriers = set(picked)
            if len(self._reachable()) > 3:
                return
        self.barriers = set()

    def _reachable(self):
        start = tuple(self.player)
        seen, queue, out = {start}, [start], []
        while queue:
            x, y = queue.pop()
            for dx, dy in self.DIRS.values():
                cell = (x + dx, y + dy)
                if (not self._inside(*cell)) or cell in seen or cell in self.barriers:
                    continue
                seen.add(cell); queue.append(cell); out.append(cell)
        return out

    def _free_cell(self):
        cells = self._reachable()
        return list(random.choice(cells)) if cells else list(self.player)

    def _adjacent(self):
        out = {}
        for action, (dx, dy) in self.DIRS.items():
            x, y = self.player[0] + dx, self.player[1] + dy
            outside = not self._inside(x, y)
            barrier = (not outside) and (x, y) in self.barriers
            out[action] = {"target": {"x": x, "y": y}, "blocked": outside or barrier,
                           "reason": "edge" if outside else ("barrier" if barrier else None)}
        return out

    def state(self):
        return {
            "game": self.id,
            "objective": "Alcancar a vitamina no menor numero de passos, sem bater em barreiras.",
            "grid": {"width": self.COLS, "height": self.ROWS, "origin": "top-left",
                     "x_direction": "increases to the right", "y_direction": "increases downward"},
            "player": {"x": self.player[0], "y": self.player[1]},
            "vitamin": {"x": self.vitamin[0], "y": self.vitamin[1]},
            "delta_to_vitamin": {"dx": self.vitamin[0] - self.player[0],
                                 "dy": self.vitamin[1] - self.player[1]},
            "adjacent_moves": self._adjacent(),
            "barriers": [{"x": x, "y": y} for x, y in sorted(self.barriers)],
            "vitamins_collected": self.score,
            "steps_taken": self.steps,
        }

    def apply(self, action, result=None):
        move = self.DIRS.get(action)
        if not move:
            return
        x, y = self.player[0] + move[0], self.player[1] + move[1]
        if self._inside(x, y) and (x, y) not in self.barriers:
            self.player = [x, y]
        else:
            self.blocked += 1
        self.steps += 1
        if self.player == self.vitamin:
            self.score += 1
            self.vitamin = self._next_spawn() if self.map else self._free_cell()

    def view(self):
        return {"cols": self.COLS, "rows": self.ROWS,
                "player": self.player, "vitamin": self.vitamin,
                "barriers": [list(b) for b in sorted(self.barriers)],
                "map": ({"id": self.map["id"], "name": self.map["name"],
                         "start": self.map["start"], "spawns": self.map["spawns"]}
                        if self.map else None)}

    def stats(self):
        return [{"label": "vitaminas", "value": self.score},
                {"label": "passos", "value": self.steps},
                {"label": "movimentos travados", "value": self.blocked,
                 "tone": "bad" if self.blocked else ""}]


class RaceGame:
    id, name = "race", "Corrida"
    funnel = "corrida_situacao"
    actions = [{"id": "accelerate", "label": "⏩ Acelerar"}, {"id": "brake", "label": "🛑 Frear"},
               {"id": "left", "label": "← Esquerda"}, {"id": "right", "label": "→ Direita"}]
    LANES, MAX_SPEED, HORIZON = 5, 100, 100
    START_SPEED = 35.0
    PARADA = 1.4          # segundos de pista limpa depois de bater
    PASSO = 0.5           # segundos de mundo por acao, no modo passo a passo

    def reset(self):
        self.distance = 0.0
        self.crashes = 0
        self.parado = 0.0
        self._recomecar()

    def _povoar_pista(self):
        """Todo carro entra la de cima: ninguem nasce colado no jogador."""
        self.obstacles = [{"lane": random.randrange(self.LANES),
                           "distance": self.HORIZON + 10.0 + i * 26}
                          for i in range(4)]

    def _bateu(self):
        """Para tudo e limpa a tela. Quem volta a andar e o _recomecar, no tick."""
        self.crashes += 1
        self.speed = 0.0
        self.obstacles = []
        self.parado = self.PARADA

    def _recomecar(self):
        self.lane, self.speed = self.LANES // 2, self.START_SPEED
        self._povoar_pista()
        self._plan_curve()

    def _plan_curve(self):
        self.curve = {"direction": random.choice(["left", "right"]),
                      "distance": 45.0 + random.random() * 45}

    def tick(self, dt):
        if self.parado > 0:
            # nao compara com zero exato: dt vem do relogio e nunca fecha a conta
            self.parado -= dt
            if self.parado <= 1e-6:
                self.parado = 0.0
                self._recomecar()
            return
        advance = self.speed * dt * 0.55
        self.distance += advance
        self.curve["distance"] -= advance
        # A batida e por cruzamento, nao por posicao: no modo passo a passo um
        # avanco pode ser maior que o carro, e um teste de janela deixaria o
        # obstaculo pular por cima do jogador sem encostar.
        for o in self.obstacles:
            antes = o["distance"]
            o["distance"] -= advance
            if antes >= -6 and o["distance"] <= 0 and o["lane"] == self.lane:
                self._bateu()
                return
        self.obstacles = [o for o in self.obstacles if o["distance"] > -14]
        while len(self.obstacles) < 4:
            self.obstacles.append({"lane": random.randrange(self.LANES),
                                   "distance": self.HORIZON + random.random() * 18})
        if self.curve["distance"] <= 0:
            self._plan_curve()
        self.speed = max(8.0, min(self.MAX_SPEED, self.speed - dt * 1.6))

    def step(self):
        """Um comando do jogador = um avanco. Depois de bater, o proximo
        comando so serve para recomecar: a pista ja volta limpa."""
        if self.parado > 0:
            self.parado = 0.0
            self._recomecar()
            return
        self.tick(self.PASSO)

    def _lanes_free(self):
        return [lane for lane in range(self.LANES)
                if not any(o["lane"] == lane and 0 <= o["distance"] < 26 for o in self.obstacles)]

    def state(self):
        ahead = sorted([o for o in self.obstacles if o["distance"] >= 0],
                       key=lambda o: o["distance"])
        in_lane = next((o for o in ahead if o["lane"] == self.lane), None)
        return {
            "game": self.id,
            "objective": "Percorrer a maior distancia sem bater, mantendo velocidade alta.",
            "crashed": self.parado > 0,
            "restarting_in": round(self.parado, 1) if self.parado else 0,
            "track": {"lanes": self.LANES, "leftmost_lane": 0, "rightmost_lane": self.LANES - 1,
                      "lane_numbers_increase": "to the right"},
            "car": {"lane": self.lane, "speed": round(self.speed), "max_speed": self.MAX_SPEED},
            "obstacles": [{"lane": o["lane"], "distance": round(o["distance"]),
                           "same_lane": o["lane"] == self.lane,
                           "lane_offset": o["lane"] - self.lane} for o in ahead[:5]],
            "nearest_in_lane": {"distance": round(in_lane["distance"])} if in_lane else None,
            "lanes_free": self._lanes_free(),
            "can_move_left": self.lane > 0,
            "can_move_right": self.lane < self.LANES - 1,
            "curve": {"direction": self.curve["direction"], "distance": round(self.curve["distance"])},
            "distance_travelled": round(self.distance),
            "crashes": self.crashes,
        }

    def apply(self, action, result=None):
        if self.parado > 0:
            return          # bateu: o carro esta parado, comando nao vale
        if action == "left":
            self.lane = max(0, self.lane - 1)
        elif action == "right":
            self.lane = min(self.LANES - 1, self.lane + 1)
        elif action == "accelerate":
            self.speed = min(self.MAX_SPEED, self.speed + 16)
        elif action == "brake":
            self.speed = max(8.0, self.speed - 26)

    def view(self):
        return {"lanes": self.LANES, "lane": self.lane, "speed": round(self.speed),
                "horizon": self.HORIZON, "curve": self.curve,
                "crashed": self.parado > 0, "crashes": self.crashes,
                "lanes_free": self._lanes_free(),
                "obstacles": [{"lane": o["lane"], "distance": round(o["distance"], 1)}
                              for o in self.obstacles]}

    def stats(self):
        return [{"label": "distância", "value": round(self.distance)},
                {"label": "velocidade",
                 "value": "bateu" if self.parado else round(self.speed),
                 "tone": "bad" if self.parado else ("" if self.speed > 70 else "warn")},
                {"label": "batidas", "value": self.crashes, "tone": "bad" if self.crashes else ""},
                {"label": "curva", "value": f"{self.curve['direction']} em {round(self.curve['distance'])}"}]


class SupportGame:
    id, name = "support", "Suporte"
    funnel = "suporte_triagem"
    actions = [{"id": "pagamentos", "label": "💳 Pagamentos"},
               {"id": "tecnico", "label": "🔧 Técnico"},
               {"id": "vendas", "label": "💰 Vendas"}]
    NOMES = ["Ana Ribeiro", "Carlos Menezes", "Juliana Alves", "Roberto Tanaka",
             "Fernanda Lima", "Diego Castro", "Patrícia Nunes", "Marcelo Braga"]
    PLANOS = ["Free", "Pro", "Enterprise"]
    CANAIS = ["e-mail", "chat", "formulário"]
    # o tema e a resposta certa: fica fora do state, so serve para pontuar
    MENSAGENS = [
        ("pagamentos", "calmo", "Boa tarde. Fiz o pagamento da fatura de março no dia 10 e o sistema ainda mostra como pendente. O comprovante está anexado. Podem verificar quando puderem?"),
        ("pagamentos", "civil", "Olha, já é a segunda vez que a cobrança duplica no meu cartão. Entendo que erros acontecem, mas está complicado explicar isso pro meu financeiro. Preciso de uma posição."),
        ("pagamentos", "bravo", "ISSO É UMA PALHAÇADA! Vocês cobraram TRÊS VEZES o mesmo valor e ninguém responde meus e-mails há uma semana. Devolvam meu dinheiro AGORA ou eu vou direto pro Procon, porcaria de empresa!"),
        ("tecnico", "calmo", "Olá! O webhook de retorno está chegando com o campo status vazio desde a versão 2.3. Consigo enviar um exemplo de payload se ajudar. Obrigado!"),
        ("tecnico", "civil", "Pessoal, a API está retornando 500 de forma intermitente desde ontem à noite. Já revisei minha integração três vezes e não achei nada do meu lado. Isso está derrubando produção."),
        ("tecnico", "bravo", "QUE MERDA DE API É ESSA?! Caiu de novo, terceira vez essa semana! Meus clientes estão me xingando por causa da INCOMPETÊNCIA de vocês. Ou consertam essa bosta hoje ou eu cancelo tudo!"),
        ("vendas", "calmo", "Bom dia! Gostaria de entender melhor a diferença entre o plano Pro e o Enterprise, principalmente quanto ao limite de requisições. Tem algum material comparativo?"),
        ("vendas", "civil", "Estou avaliando migrar pro Enterprise mas não achei o preço por assento em lugar nenhum do site. Já perdi um bom tempo procurando e o chat não me respondeu. Podem me passar?"),
        ("vendas", "bravo", "Vocês aumentaram o preço em 40% sem AVISAR NINGUÉM e ainda tiveram a cara de pau de mandar e-mail de \"novidades\"! Isso é um absurdo, é roubo escancarado. Quero falar com alguém que decide, AGORA."),
    ]

    def reset(self):
        self.atendidos = self.acertos = 0
        self.soma_frustracao = 0.0
        self.por_equipe = {"pagamentos": 0, "tecnico": 0, "vendas": 0}
        self.novo_cliente()

    def novo_cliente(self):
        tema, tom, texto = random.choice(self.MENSAGENS)
        self.ticket = {"id": f"TK-{random.randint(1000, 9999)}",
                       "cliente": random.choice(self.NOMES),
                       "plano": random.choice(self.PLANOS),
                       "canal": random.choice(self.CANAIS),
                       "mensagem": texto}
        self.tema_real, self.tom_real = tema, tom
        self.frustracao = self.urgencia = None
        self.frustracao_label = None
        self.encaminhado = None
        self.resolvido = False
        self.acertou = None

    def prepare(self):
        if self.resolvido:
            self.novo_cliente()

    def state(self):
        return {
            "game": self.id,
            "objective": "Encaminhar o ticket para a equipe correta.",
            "equipes": ["pagamentos", "tecnico", "vendas"],
            "ticket": {"id": self.ticket["id"], "canal": self.ticket["canal"],
                       "plano_do_cliente": self.ticket["plano"],
                       "cliente": self.ticket["cliente"],
                       "mensagem": self.ticket["mensagem"]},
        }

    def apply(self, action, result=None):
        signals = (result or {}).get("signals") or {}
        frustracao = signals.get("nivel_de_frustracao") or {}
        urgencia = signals.get("urgencia") or {}
        value = frustracao.get("value")
        self.frustracao = value if isinstance(value, (int, float)) else None
        self.frustracao_label = frustracao.get("label")
        urg = urgencia.get("value")
        self.urgencia = urg if isinstance(urg, (int, float)) else None
        self.encaminhado = action
        self.resolvido = True
        self.acertou = action == self.tema_real
        self.atendidos += 1
        self.acertos += 1 if self.acertou else 0
        if self.frustracao is not None:
            self.soma_frustracao += self.frustracao
        if action in self.por_equipe:
            self.por_equipe[action] += 1

    def view(self):
        return {"ticket": self.ticket, "frustracao": self.frustracao,
                "frustracao_label": self.frustracao_label, "urgencia": self.urgencia,
                "resolvido": self.resolvido, "encaminhado": self.encaminhado,
                "acertou": self.acertou,
                "tema_real": self.tema_real if self.resolvido else None,
                "por_equipe": self.por_equipe}

    def stats(self):
        pct = round(self.acertos / self.atendidos * 100) if self.atendidos else 0
        # nada de "tom real" nem media de frustracao: entregariam a leitura
        # que a tela quer deixar para quem esta assistindo
        return [{"label": "tickets", "value": self.atendidos},
                {"label": "roteados certo", "value": f"{self.acertos}/{self.atendidos} ({pct}%)",
                 "tone": "bad" if self.atendidos and pct < 70 else ""}]


class StreetFootballGame:
    """Nivel 1 do racha: so o jogador, a bola e o gol.

    Sem carro passando, sem cachorro levando a bola. A missao inteira e:
    achar a bola, levar ate a pequena area e chutar de la. Fez gol, nasce
    outra bola em outro canto e recomeca.

    O campo e 120x80 em unidades proprias — numero redondo le melhor no
    estado do que pixel de tela, e quem escreve a pergunta para o Jev vai
    falar desses numeros.
    """

    id, name = "street_football", "Futebol de rua"
    funnel = "futebol_gol"
    actions = [{"id": "up", "label": "\u2191 Cima"}, {"id": "down", "label": "\u2193 Baixo"},
               {"id": "left", "label": "\u2190 Esquerda"}, {"id": "right", "label": "\u2192 Direita"},
               {"id": "chutar", "label": "\u26bd Chutar"}]

    LARGURA, ALTURA = 120.0, 80.0
    GOL_TOPO, GOL_BASE = 30.0, 50.0        # os dois chinelos
    AREA_X = 100.0                          # a pequena area comeca aqui
    AREA_TOPO, AREA_BASE = 24.0, 56.0
    VELOCIDADE = 26.0                       # unidades por segundo
    RAIO_CONTROLE = 4.0                     # perto assim, a bola e dele
    PASSO = 0.5                             # segundos de mundo por acao, no passo a passo

    DIRECOES = {"up": (0.0, -1.0), "down": (0.0, 1.0),
                "left": (-1.0, 0.0), "right": (1.0, 0.0)}

    def reset(self):
        self.gols = 0
        self.chutes_perdidos = 0
        self.passos = 0
        self.rumo = None                    # ultima direcao mandada
        self.aviso = ""                     # o que acabou de acontecer, para a tela
        self.aviso_ate = 0.0
        self.player = [20.0, self.ALTURA / 2]
        self._nova_bola()

    # ---- bola ----
    def _nova_bola(self):
        """Longe do gol e longe do jogador: senao a rodada acaba sem jogo."""
        for _ in range(40):
            x = random.uniform(10, self.AREA_X - 10)
            y = random.uniform(8, self.ALTURA - 8)
            if math.dist((x, y), self.player) > 25:
                self.bola = [x, y]
                return
        self.bola = [random.uniform(10, 40), random.uniform(8, self.ALTURA - 8)]

    def _tem_a_bola(self):
        return math.dist(self.player, self.bola) <= self.RAIO_CONTROLE

    def _na_area(self):
        return (self.player[0] >= self.AREA_X
                and self.AREA_TOPO <= self.player[1] <= self.AREA_BASE)

    def _falar(self, texto):
        self.aviso = texto
        self.aviso_ate = time.time() + 1.6

    # ---- tempo ----
    def tick(self, dt):
        if not self.rumo:
            return
        dx, dy = self.DIRECOES[self.rumo]
        andar = self.VELOCIDADE * dt
        self.player[0] = max(2.0, min(self.LARGURA - 2.0, self.player[0] + dx * andar))
        self.player[1] = max(2.0, min(self.ALTURA - 2.0, self.player[1] + dy * andar))
        # a bola dominada anda junto, um passo a frente do jogador
        if self._tem_a_bola():
            self.bola[0] = max(1.0, min(self.LARGURA - 1.0, self.player[0] + dx * 2.2))
            self.bola[1] = max(1.0, min(self.ALTURA - 1.0, self.player[1] + dy * 2.2))

    def step(self):
        self.tick(self.PASSO)

    # ---- acoes ----
    def apply(self, action, result=None):
        self.passos += 1
        if action == "chutar":
            self._chutar()
            return
        if action in self.DIRECOES:
            self.rumo = action

    def _chutar(self):
        if not self._tem_a_bola():
            self._falar("chutou o vento")
            self.chutes_perdidos += 1
            return
        if self._na_area():
            self.gols += 1
            self._falar("GOL!")
            self.rumo = None
            self.player = [20.0, self.ALTURA / 2]
            self._nova_bola()
            return
        # chute de longe: a bola vai para longe e ele tem que buscar de novo
        self.chutes_perdidos += 1
        self._falar("chutou de longe, foi para fora")
        self.rumo = None
        self._nova_bola()

    # ---- leitura ----
    def _rumo_ate(self, alvo):
        """Em que direcao esta o alvo, no vocabulario das acoes."""
        dx, dy = alvo[0] - self.player[0], alvo[1] - self.player[1]
        if abs(dx) < 1.5 and abs(dy) < 1.5:
            return "em cima"
        if abs(dx) >= abs(dy):
            return "right" if dx > 0 else "left"
        return "down" if dy > 0 else "up"

    def state(self):
        com_bola = self._tem_a_bola()
        gol = [self.LARGURA, (self.GOL_TOPO + self.GOL_BASE) / 2]
        return {
            "game": self.id,
            "objective": ("Pegar a bola, levar ate a pequena area do gol e chutar de dentro "
                          "dela. Chute de fora da area nao vale e manda a bola para longe."),
            "field": {"width": self.LARGURA, "height": self.ALTURA,
                      "x_cresce": "para a direita, na direcao do gol",
                      "y_cresce": "para baixo"},
            "goal": {"x": self.LARGURA, "y_top": self.GOL_TOPO, "y_bottom": self.GOL_BASE},
            "small_box": {"x_min": self.AREA_X, "y_top": self.AREA_TOPO,
                          "y_bottom": self.AREA_BASE,
                          "nota": "so vale gol chutando de dentro desta caixa"},
            "player": {"x": round(self.player[0], 1), "y": round(self.player[1], 1),
                       "andando_para": self.rumo},
            "ball": {"x": round(self.bola[0], 1), "y": round(self.bola[1], 1)},
            "com_a_bola": com_bola,
            "dentro_da_pequena_area": self._na_area(),
            "pronto_para_chutar": com_bola and self._na_area(),
            "bola": {"distancia": round(math.dist(self.player, self.bola), 1),
                     "direcao": self._rumo_ate(self.bola)},
            "pequena_area": {"distancia": round(math.dist(self.player, [self.AREA_X, gol[1]]), 1),
                             "direcao": self._rumo_ate([self.AREA_X + 8, gol[1]])},
            "gols": self.gols,
            "chutes_perdidos": self.chutes_perdidos,
        }

    def view(self):
        return {
            "width": self.LARGURA, "height": self.ALTURA,
            "goal": {"top": self.GOL_TOPO, "bottom": self.GOL_BASE},
            "box": {"x": self.AREA_X, "top": self.AREA_TOPO, "bottom": self.AREA_BASE},
            "player": {"x": self.player[0], "y": self.player[1], "rumo": self.rumo},
            "ball": {"x": self.bola[0], "y": self.bola[1]},
            "com_a_bola": self._tem_a_bola(),
            "na_area": self._na_area(),
            "gols": self.gols,
            "aviso": self.aviso if time.time() < self.aviso_ate else "",
        }

    def stats(self):
        return [{"label": "gols", "value": self.gols},
                {"label": "chutes perdidos", "value": self.chutes_perdidos,
                 "tone": "bad" if self.chutes_perdidos else ""},
                {"label": "passos", "value": self.passos},
                {"label": "bola", "value": "dominada" if self._tem_a_bola() else "solta",
                 "tone": "" if self._tem_a_bola() else "warn"}]


GAMES = {g.id: g for g in (VitaminGame(), RaceGame(), SupportGame(),
                           StreetFootballGame())}


def _load_session():
    """O que estava escolhido da ultima vez. Nunca derruba o servidor."""
    try:
        dados = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        return dados if isinstance(dados, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_session(dados):
    try:
        SESSION_FILE.write_text(json.dumps(dados, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass       # disco somente leitura: o servidor segue, so nao lembra


# ==========================================================================
# O mundo: roda sozinho, guarda o estado, aceita acoes de quem chegar.
# ==========================================================================

class World:
    """Estado vivo do jogo. Nenhum cliente e privilegiado."""

    def __init__(self):
        self.game = GAMES["vitamin_hunt"]
        self.game.reset()
        self.frame = 0
        self.paused = False
        self.step_mode = False
        self.lock = threading.Lock()
        self.history = deque(maxlen=40)   # quem mandou o que, recentemente
        self.clients = {}                 # quem ja pediu frame ou mandou acao
        self._restaurar()
        threading.Thread(target=self._ticker, daemon=True).start()

    # ---- lembrar onde parou ----
    def _restaurar(self):
        sessao = _load_session()
        jogo = sessao.get("game")
        if jogo in GAMES:
            self.game = GAMES[jogo]
            self.game.reset()
        mapa_id = sessao.get("map_id") or ""
        if mapa_id:
            mapa = load_maps().get(mapa_id)
            if mapa:
                GAMES["vitamin_hunt"].load_map(mapa)
        self.paused = bool(sessao.get("paused"))
        self.step_mode = bool(sessao.get("step_mode"))
        self.restaurado = {"game": self.game.id, "map_id": mapa_id if mapa_id else "",
                           "paused": self.paused, "step_mode": self.step_mode,
                           "vindo_do_arquivo": bool(sessao)}

    def _lembrar(self):
        mapa = GAMES["vitamin_hunt"].map
        _save_session({"game": self.game.id,
                       "map_id": mapa["id"] if mapa else "",
                       "paused": self.paused,
                       "step_mode": self.step_mode})

    # ---- clientes ----
    def _touch(self, name, address, what):
        entry = self.clients.setdefault(name, {
            "name": name, "address": address, "first_seen": time.time(),
            "frames": 0, "actions": 0,
        })
        entry["address"] = address
        entry["last_seen"] = time.time()
        entry[what] += 1

    # ---- leitura ----
    def frame_payload(self, client=None, address="", advance=False):
        """advance=True e um pedido para jogar: jogos com fila passam para a
        proxima rodada. Observadores (/api/view) leem sem avancar nada."""
        with self.lock:
            if client:
                self._touch(client, address, "frames")
            if advance:
                prepare = getattr(self.game, "prepare", None)
                if prepare:
                    prepare()
            # so faz sentido em jogo que anda sozinho; nos de turno o mundo
            # ja espera o cliente por natureza
            por_passo = self.step_mode and self.anda_sozinho()
            vista = self.game.view()
            vista["step_mode"] = por_passo
            return {
                "frame": self.frame,
                "game": self.game.id,
                "at": round(time.time(), 3),
                "paused": self.paused,
                "step_mode": por_passo,
                "stepable": self.anda_sozinho(),
                "state": self.game.state(),
                "actions": self.game.actions,
                "stats": self.game.stats(),
                "view": vista,
            }

    # ---- escrita ----
    def apply_action(self, action, by="anonimo", on_frame=None, address="", signals=None):
        """Aplica uma acao venha de quem vier. Frame velho e avisado, nao recusado.

        `signals` sao anotacoes opcionais do cliente. O jogo nao depende delas
        para funcionar; quando vem, usa para enriquecer o que mostra na tela.
        """
        valid = [a["id"] for a in self.game.actions]
        if action not in valid:
            raise ValueError(f"Acao {action!r} nao existe em {self.game.id}. Validas: {valid}")
        with self.lock:
            stale = on_frame is not None and int(on_frame) != self.frame
            decided_on = on_frame if on_frame is not None else self.frame
            self.game.apply(action, {"signals": signals or {}})
            # No passo a passo o mundo so anda aqui: primeiro o comando, depois
            # o avanco — o jogador vira e entao o carro percorre o trecho.
            if self.step_mode:
                passo = getattr(self.game, "step", None)
                if passo:
                    passo()
            self.frame += 1
            self._touch(by, address, "actions")
            self.history.appendleft({
                "frame": self.frame, "action": action, "by": by,
                "stale": stale, "decided_on": decided_on,
                "signals": signals or {}, "at": round(time.time(), 3),
            })
            payload = {
                "ok": True, "applied": action, "frame": self.frame, "stale": stale,
                "game": self.game.id, "state": self.game.state(),
                "stats": self.game.stats(), "view": self.game.view(),
            }
        return payload

    def contract(self):
        """O que da para ler e o que da para mandar. Nao avanca nada:
        quem quer conhecer o jogo nao deve gastar uma rodada para isso."""
        with self.lock:
            return {
                "game": self.game.id,
                "name": self.game.name,
                "actions": self.game.actions,
                "state": self.game.state(),
                "note": ("Estado de exemplo, tirado da rodada atual. "
                         "As chaves sao sempre as mesmas; os valores mudam a cada frame."),
            }

    def carregar_mapa(self, map_id):
        """So o Vitamina tem mapa; para os outros o comando nao faz sentido."""
        jogo = GAMES["vitamin_hunt"]
        mapa = load_maps().get(str(map_id)) if map_id else None
        if map_id and not mapa:
            raise ValueError(f"Mapa desconhecido: {map_id!r}")
        with self.lock:
            jogo.load_map(mapa)
            if self.game is jogo:
                self.frame = 0
                self.history.clear()
        self._lembrar()

    def select(self, game_id):
        if game_id not in GAMES:
            raise ValueError(f"Jogo desconhecido: {game_id!r}")
        with self.lock:
            self.game = GAMES[game_id]
            self.game.reset()
            self.frame = 0
            self.history.clear()
        self._lembrar()

    def reset(self):
        with self.lock:
            self.game.reset()
            self.frame = 0
            self.history.clear()

    def anda_sozinho(self):
        """O jogo tem movimento proprio? So esses aceitam o passo a passo."""
        return callable(getattr(self.game, "step", None))

    def _ticker(self):
        """Jogos com movimento continuo andam sozinhos, com ou sem cliente.
        No passo a passo o relogio para: quem anda o mundo e a acao."""
        last = time.perf_counter()
        while True:
            now = time.perf_counter()
            dt, last = min(0.05, now - last), now
            tick = getattr(self.game, "tick", None)
            if tick and not self.paused and not self.step_mode:
                with self.lock:
                    tick(dt)
            time.sleep(0.05)

    def view(self):
        payload = self.frame_payload()
        recent = list(self.history)
        mapa_atual = GAMES["vitamin_hunt"].map
        payload.update({
            "games": [{"id": g.id, "name": g.name} for g in GAMES.values()],
            "maps": list(load_maps().values()),
            "map_id": mapa_atual["id"] if mapa_atual else "",
            "game_name": self.game.name,
            "history": recent,
            "last": recent[0] if recent else None,
            "clients": sorted(
                ({**c, "seconds_since_last": round(time.time() - c["last_seen"], 1)}
                 for c in self.clients.values()),
                key=lambda c: c["seconds_since_last"]),
        })
        return payload


world = World()


def handshake():
    return {
        "ok": True,
        "service": SERVICE,
        "version": VERSION,
        "role": "servidor de jogo — passivo, espera clientes",
        "games": [{"id": g.id, "name": g.name, "actions": [a["id"] for a in g.actions]}
                  for g in GAMES.values()],
        "active": world.game.id,
        "frame": world.frame,
        "protocol": {
            "read": "GET /api/frame -> {frame, game, state, actions, stats, view}",
            "write": ('POST /api/action {"action": "<id>", "by": "<seu nome>", '
                      '"frame": <n opcional>, "signals": {<anotacoes opcionais>}}'),
            "observe": "GET /api/view -> mesmo estado, sem avancar fila nenhuma",
            "contract": ("GET /api/contract -> {game, actions, state} com um estado de "
                         "exemplo. E por aqui que se descobre o que mandar e o que ler."),
            "note": ("Qualquer cliente pode pedir frame e mandar acao a qualquer momento. "
                     "Mandar 'frame' faz o jogo avisar se voce decidiu em cima de um frame velho. "
                     "GET /api/frame avanca a fila de jogos que tem uma; use /api/view para so olhar."),
        },
        "no_api_key": "Este sistema nao tem chave de API e nao fala com nenhum modelo.",
    }



# Servir arquivo e conveniencia para abrir a pagina, nao um servidor de
# arquivos. Sem essa lista, qualquer .py largado na pasta sai pela porta.
EXTENSOES_PUBLICAS = {
    ".html", ".htm", ".css", ".js", ".mjs", ".map", ".json",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".woff", ".woff2", ".ttf", ".mp3", ".ogg", ".wav", ".txt",
}


def pode_servir(caminho):
    """Dotfile nunca sai (.env, .jev_*). Fora isso, so extensao da lista."""
    partes = [p for p in unquote(caminho).replace("\\", "/").split("/") if p]
    if any(p.startswith(".") for p in partes):
        return False
    if not partes:
        return True                      # a raiz redireciona, nao serve arquivo
    return Path(partes[-1]).suffix.lower() in EXTENSOES_PUBLICAS


def origem_local(origem):
    """CORS so para quem veio da propria maquina.

    Com `*`, qualquer site aberto no navegador conseguia ler estas rotas e,
    pior, disparar POST — gastando chamada de API ou mexendo no jogo.
    """
    if not origem:
        return None
    try:
        host = urlparse(origem).hostname
    except ValueError:
        return None
    return origem if host in ("127.0.0.1", "localhost", "::1", "[::1]") else None

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def _json(self, status, payload):
        out = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        permitida = origem_local(self.headers.get("Origin"))
        if permitida:
            self.send_header("Access-Control-Allow-Origin", permitida)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _client_name(self):
        return (self.headers.get("X-Client")
                or self.headers.get("User-Agent", "").split("/")[0]
                or "anonimo")[:60]

    def do_OPTIONS(self):
        permitida = origem_local(self.headers.get("Origin"))
        if not permitida:
            self.send_error(403); return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", permitida)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Client")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self.send_response(302); self.send_header("Location", "/game.html"); self.end_headers(); return
        if path == "/api/handshake":
            self._json(200, handshake()); return
        if path == "/api/frame":
            self._json(200, world.frame_payload(
                self._client_name(), self.client_address[0], advance=True)); return
        if path == "/api/view":
            self._json(200, world.view()); return
        if path == "/api/contract":
            self._json(200, world.contract()); return
        if path == "/api/maps":
            self._json(200, {"maps": list(load_maps().values())}); return
        if not pode_servir(path):
            self.send_error(404); return
        super().do_GET()

    def do_POST(self):
        if self.path not in ("/api/action", "/api/control", "/api/maps", "/api/maps/delete"):
            self.send_error(404); return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/action":
                by = str(data.get("by") or self._client_name())[:60]
                self._json(200, world.apply_action(
                    str(data.get("action") or ""), by, data.get("frame"),
                    self.client_address[0], data.get("signals")))
                return
            if self.path == "/api/maps":
                salvo = save_map(data)
                world.carregar_mapa(salvo["id"])
                self._json(200, {"ok": True, "map": salvo, **world.view()}); return
            if self.path == "/api/maps/delete":
                alvo = str(data.get("id") or "")
                delete_map(alvo)
                if (GAMES["vitamin_hunt"].map or {}).get("id") == alvo:
                    world.carregar_mapa(None)
                self._json(200, {"ok": True, **world.view()}); return

            cmd, value = data.get("cmd"), data.get("value")
            if cmd == "map":
                world.carregar_mapa(str(value or ""))
            elif cmd == "game":
                world.select(str(value))
            elif cmd == "reset":
                world.reset()
            elif cmd == "pause":
                world.paused = bool(value)
                world._lembrar()
            elif cmd == "step_mode":
                world.step_mode = bool(value)
                world._lembrar()
            else:
                raise ValueError(f"comando desconhecido: {cmd!r}")
            self._json(200, {"ok": True, **world.view()})
        except Exception as exc:
            self._json(400, {"ok": False, "error": str(exc)})

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/"):
            super().log_message(fmt, *args)


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main():
    port = int(os.getenv("GAME_PORT", os.getenv("PORT", "8100")))
    print("=" * 62)
    print(f"  SISTEMA DE JOGOS  \u00b7  http://127.0.0.1:{port}")
    print("=" * 62)
    print(f"  jogos      : {', '.join(GAMES)}")
    restaurado = world.restaurado
    mapa = GAMES["vitamin_hunt"].map
    if restaurado["vindo_do_arquivo"]:
        detalhe = f" (mapa {mapa['name']})" if mapa else ""
        print(f"  ativo      : {world.game.id}{detalhe}  — retomado de onde parou")
    else:
        print(f"  ativo      : {world.game.id}")
    print("  papel      : servidor passivo — espera clientes, nao chama ninguem")
    print(f"  frame      : GET  http://127.0.0.1:{port}/api/frame")
    print(f"  acao       : POST http://127.0.0.1:{port}/api/action")
    print(f"  contrato   : GET  http://127.0.0.1:{port}/api/contract")
    print("  este processo nao tem chave de API.")
    print("=" * 62)
    QuietServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
