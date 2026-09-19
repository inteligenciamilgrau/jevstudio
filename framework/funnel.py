#!/usr/bin/env python3
"""Funis de perguntas ao Jev que terminam em uma acao do jogo.

Um funil e um grafo pequeno. Cada NO e um conjunto de PERGUNTAS que saem
juntas, num unico request — o Jev responde o lote inteiro de uma vez, entao
perguntar mais custa quase nada.

Cada resposta possivel de cada pergunta e uma SAIDA, e qualquer saida pode ser
ligada a um proximo no ou a uma acao do jogo. Saida sem ligacao simplesmente
nao aciona nada. Quando o no roda, o motor percorre as perguntas na ordem e usa
a PRIMEIRA cuja resposta caiu numa saida ligada; o resto vira leitura, que
aparece no trace e segue para o jogo junto com a acao.

Cada pergunta tem um tipo, e o tipo decide como ela e respondida e, quando ela
e a que ramifica, quais saidas o no tem:

    choice  -> escolhe uma das opcoes descritas   -> uma saida por opcao
    noul    -> responde um numero de 0 a 1        -> duas saidas, por limiar
    score   -> da uma nota sobre uma escala       -> uma saida por nivel

O jogo nao aparece aqui: ele declara as proprias acoes a cada request, entao o
mesmo motor serve para uma corrida e para uma fila de suporte.

O motor e agnostico de transporte: quem chama injeta um `call_jev(payload)`
que devolve `(data, latency_ms)`, o que mantem a chave de API fora deste modulo.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FUNNELS_FILE = ROOT / ".jev_funnels.json"
MAX_FUNNEL_HOPS = 12

# A API aceita exatamente estes tipos (um `type` invalido devolve 422 listando
# os validos). `noul` e o booleano probabilistico do Jev: responde um float de
# 0 a 1, nao true/false. `score` avalia contra uma lista ordenada e tambem
# responde um float, entao 1.68 quer dizer "entre o nivel 1 e o 2, mais perto
# do 2".
QUESTION_TYPES = ("choice", "noul", "score")
DEFAULT_THRESHOLD = 0.5
MAX_QUESTIONS_PER_NODE = 8


# ---------------------------------------------------------------------------
# Forma de um no
# ---------------------------------------------------------------------------

def branch_ids(question):
    """As respostas possiveis de uma pergunta. Cada uma pode virar uma ligacao."""
    if not question:
        return []
    tipo = question.get("type")
    if tipo == "noul":
        return ["sim", "nao"]
    if tipo == "score":
        return [f"nivel_{i}" for i in range(len(question.get("criteria") or []))]
    return list((question.get("criteria") or {}).keys())


def branch_label(question, branch_id):
    """Como essa saida aparece para quem le."""
    if not question:
        return branch_id
    tipo = question.get("type")
    if tipo == "noul":
        limiar = question.get("threshold", DEFAULT_THRESHOLD)
        return f">= {limiar}" if branch_id == "sim" else f"< {limiar}"
    if tipo == "score":
        try:
            i = int(branch_id.split("_")[1])
        except (IndexError, ValueError):
            return branch_id
        niveis = question.get("criteria") or []
        return f"{i} · {niveis[i]}" if i < len(niveis) else branch_id
    return branch_id


def _sanitize_question(raw):
    if not isinstance(raw, dict):
        return None
    tipo = str(raw.get("type") or "").strip()
    if tipo not in QUESTION_TYPES:
        return None
    try:
        limiar = max(0.0, min(1.0, float(raw.get("threshold", DEFAULT_THRESHOLD))))
    except (TypeError, ValueError):
        limiar = DEFAULT_THRESHOLD
    pergunta = {
        "type": tipo,
        "label": str(raw.get("label") or "").strip()[:80],
        "instructions": str(raw.get("instructions") or "").strip()[:1200],
        "threshold": round(limiar, 3),
    }
    if tipo == "score":
        bruto = raw.get("criteria")
        bruto = bruto if isinstance(bruto, list) else []
        pergunta["criteria"] = [str(n).strip()[:200] for n in bruto[:7] if str(n).strip()]
    elif tipo == "choice":
        bruto = raw.get("criteria")
        bruto = bruto if isinstance(bruto, dict) else {}
        pergunta["criteria"] = {str(k).strip()[:60]: str(v).strip()[:400]
                                for k, v in list(bruto.items())[:8] if str(k).strip()}
    else:
        pergunta["criteria"] = None
    return pergunta


def _migrate_node(node):
    """Converte o formato antigo (type + options/levels + signals) no novo.

    Vale para funis salvos antes de o no virar um conjunto de perguntas.
    """
    if "questions" in node:
        return node
    tipo = node.get("type", "choice")
    principal = {
        "type": tipo,
        "label": str(node.get("label") or "pergunta principal"),
        "instructions": node.get("instructions", ""),
        "threshold": node.get("threshold", DEFAULT_THRESHOLD),
    }
    opcoes = node.get("options") or {}
    if tipo == "choice":
        principal["criteria"] = {oid: (o.get("description") or oid) for oid, o in opcoes.items()}
    elif tipo == "score":
        principal["criteria"] = list(node.get("levels") or [])

    questions = {"principal": principal}
    for sid, sinal in (node.get("signals") or {}).items():
        if sid != "principal":
            questions[sid] = sinal

    return {
        **node,
        "questions": questions,
        "routes_by": "principal",
        "outputs": {oid: {"to": o.get("to"), "emit": o.get("emit")} for oid, o in opcoes.items()},
    }


def _migrate_outputs(node, questions):
    """Converte saidas planas (de quando so uma pergunta ramificava) em
    saidas por pergunta: {qid: {resposta: ligacao}}."""
    brutas = node.get("outputs")
    if not isinstance(brutas, dict) or not brutas:
        return {}
    # ja esta no formato novo quando o primeiro valor e um dicionario de ligacoes
    primeiro = next(iter(brutas.values()), None)
    if isinstance(primeiro, dict) and not {"to", "emit"} & set(primeiro):
        return brutas
    dono = node.get("routes_by")
    if dono not in questions:
        dono = next(iter(questions), None)
    return {dono: brutas} if dono else {}


# ---------------------------------------------------------------------------
# Funis que ja vem prontos
# ---------------------------------------------------------------------------

DEFAULT_FUNNELS = {
    "vitamina_eixo": {
        "id": "vitamina_eixo",
        "name": "Vitamina: eixo, depois sentido",
        "game": "vitamin_hunt",
        "entry": "eixo",
        "nodes": {
            "eixo": {
                "label": "Qual eixo?",
                "questions": {
                    "eixo": {
                        "type": "choice",
                        "label": "Qual eixo",
                        "instructions": (
                            "Decida em qual EIXO o player deve se mover agora para alcancar a "
                            "vitamina. Nao escolha o sentido final ainda, apenas o eixo. "
                            "Use delta_to_vitamin e adjacent_moves. Um eixo cujos dois sentidos "
                            "estejam blocked nao serve para nada."
                        ),
                        "criteria": {
                            "horizontal": ("Eixo X (esquerda/direita). Prefira quando |dx| for maior "
                                           "que |dy|, ou quando os dois sentidos verticais estiverem blocked."),
                            "vertical": ("Eixo Y (cima/baixo). Prefira quando |dy| for maior que |dx|, "
                                         "ou quando os dois sentidos horizontais estiverem blocked."),
                        },
                    },
                },
                "routes_by": "eixo",
                "outputs": {"horizontal": {"to": "dir_h", "emit": None},
                            "vertical": {"to": "dir_v", "emit": None}},
                "x": 40, "y": 170,
            },
            "dir_h": {
                "label": "Esquerda ou direita?",
                "questions": {
                    "sentido": {
                        "type": "choice",
                        "label": "Sentido horizontal",
                        "instructions": ("O eixo horizontal ja foi escolhido. Decida o sentido. "
                                         "Nunca escolha um sentido cujo adjacent_moves esteja blocked."),
                        "criteria": {"left": "Uma celula para a esquerda (x - 1).",
                                     "right": "Uma celula para a direita (x + 1)."},
                    },
                },
                "routes_by": "sentido",
                "outputs": {"left": {"to": None, "emit": "left"},
                            "right": {"to": None, "emit": "right"}},
                "x": 400, "y": 50,
            },
            "dir_v": {
                "label": "Cima ou baixo?",
                "questions": {
                    "sentido": {
                        "type": "choice",
                        "label": "Sentido vertical",
                        "instructions": ("O eixo vertical ja foi escolhido. Decida o sentido. "
                                         "Nunca escolha um sentido cujo adjacent_moves esteja blocked."),
                        "criteria": {"up": "Uma celula para cima (y - 1).",
                                     "down": "Uma celula para baixo (y + 1)."},
                    },
                },
                "routes_by": "sentido",
                "outputs": {"up": {"to": None, "emit": "up"},
                            "down": {"to": None, "emit": "down"}},
                "x": 400, "y": 290,
            },
        },
    },
    "corrida_situacao": {
        "id": "corrida_situacao",
        "name": "Corrida: situacao, depois reacao",
        "game": "race",
        "entry": "situacao",
        "nodes": {
            "situacao": {
                "label": "O que domina a cena?",
                "questions": {
                    "situacao": {
                        "type": "choice",
                        "label": "Situacao",
                        "instructions": ("Leia a pista e classifique a situacao MAIS URGENTE agora. "
                                         "Use obstacles (o de menor distance primeiro), curve, lane e "
                                         "speed. Perigo imediato sempre vence conforto de velocidade."),
                        "criteria": {
                            "reta_livre": ("Nenhum obstaculo perto na minha faixa e nenhuma curva "
                                           "iminente. Da para ganhar velocidade com seguranca."),
                            "curva": ("Existe uma curva proxima e nenhum obstaculo imediato na minha "
                                      "faixa. Preciso me posicionar para ela."),
                            "risco": "Existe um obstaculo proximo na minha faixa. Preciso reagir agora.",
                        },
                    },
                },
                "routes_by": "situacao",
                "outputs": {"reta_livre": {"to": None, "emit": "accelerate"},
                            "curva": {"to": "lado_curva", "emit": None},
                            "risco": {"to": "reagir", "emit": None}},
                "x": 40, "y": 180,
            },
            "lado_curva": {
                "label": "Para que lado?",
                "questions": {
                    "lado": {
                        "type": "choice",
                        "label": "Lado da curva",
                        "instructions": ("A curva ja foi identificada. Escolha para qual lado mover o "
                                         "carro, usando curve.direction e a faixa atual. Nao saia da pista."),
                        "criteria": {"esquerda": "Mover uma faixa para a esquerda.",
                                     "direita": "Mover uma faixa para a direita."},
                    },
                },
                "routes_by": "lado",
                "outputs": {"esquerda": {"to": None, "emit": "left"},
                            "direita": {"to": None, "emit": "right"}},
                "x": 400, "y": 50,
            },
            "reagir": {
                "label": "Como reagir?",
                "questions": {
                    "reacao": {
                        "type": "choice",
                        "label": "Reacao",
                        "instructions": ("Ha um obstaculo na faixa. Escolha a reacao. Desviar so vale "
                                         "se a faixa de destino existir e aparecer em lanes_free; se os "
                                         "dois lados estiverem ocupados, freie."),
                        "criteria": {
                            "frear": ("Reduzir a velocidade e manter a faixa. Escolha segura quando os "
                                      "dois lados estao ocupados."),
                            "desviar_esq": "Desviar uma faixa para a esquerda. So se essa faixa existir e estiver livre.",
                            "desviar_dir": "Desviar uma faixa para a direita. So se essa faixa existir e estiver livre.",
                        },
                    },
                },
                "routes_by": "reacao",
                "outputs": {"frear": {"to": None, "emit": "brake"},
                            "desviar_esq": {"to": None, "emit": "left"},
                            "desviar_dir": {"to": None, "emit": "right"}},
                # Desviar para uma faixa ocupada custa uma batida; frear nao custa nada.
                # Se o Jev nao tiver conviccao, o funil frea.
                "min_confidence": 0.6,
                "fallback": "frear",
                "x": 400, "y": 270,
            },
        },
    },
    "futebol_gol": {
        "id": "futebol_gol",
        "name": "Futebol: buscar, levar e chutar",
        "game": "street_football",
        "entry": "jogada",
        "nodes": {
            "jogada": {
                "label": "A jogada agora",
                # Duas perguntas no mesmo request. A ordem importa: a primeira
                # resposta que cair numa saida ligada decide. Entao o "chutar
                # agora" vem antes do rumo — quando ele diz sim, o rumo vira
                # so leitura e nem chega a acionar nada.
                "questions": {
                    "chutar_agora": {
                        "type": "noul",
                        "label": "Chutar agora",
                        "instructions": (
                            "O jogador esta com a bola dominada E dentro da pequena area "
                            "(small_box), ou seja, `pronto_para_chutar` e verdadeiro. "
                            "Responda alto so quando as duas coisas valem ao mesmo tempo: "
                            "chutar de fora da area manda a bola para longe e perde a jogada."
                        ),
                        "threshold": 0.6,
                    },
                    "rumo": {
                        "type": "choice",
                        "label": "Para onde correr",
                        "instructions": (
                            "Para que lado o jogador deve correr neste frame. Se ele ainda "
                            "nao esta com a bola (`com_a_bola` falso), va na direcao da bola, "
                            "em `bola.direcao`. Se ja esta com a bola, leve-a para a pequena "
                            "area do gol, em `pequena_area.direcao`. Lembre que x cresce para "
                            "a direita (onde fica o gol) e y cresce para baixo. Os campos "
                            "`distancia_x` e `distancia_y` dizem quanto falta em cada eixo, "
                            "com sinal: positivo e para a direita e para baixo. As diagonais "
                            "existem e sao quase sempre melhores: andar so em cruz faz o "
                            "caminho virar escada. Quando os dois eixos tem sobra parecida, a "
                            "resposta e uma diagonal. Se o campo `direcao` que voce leu ja e "
                            "uma diagonal, responda exatamente ela."
                        ),
                        "criteria": {
                            "up": "So para cima: o alvo esta quase na mesma coluna, com y menor",
                            "down": "So para baixo: o alvo esta quase na mesma coluna, com y maior",
                            "left": "So para a esquerda: o alvo esta quase na mesma linha, com x menor",
                            "right": "So para a direita, na direcao do gol, quase na mesma linha",
                            "up_left": "Diagonal: o alvo esta acima E a esquerda",
                            "up_right": "Diagonal: o alvo esta acima E a direita",
                            "down_left": "Diagonal: o alvo esta abaixo E a esquerda",
                            "down_right": "Diagonal: o alvo esta abaixo E a direita",
                        },
                    },
                },
                "outputs": {
                    "chutar_agora": {"sim": {"to": None, "emit": "chutar"},
                                     "nao": {"to": None, "emit": None}},
                    "rumo": {"up": {"to": None, "emit": "up"},
                             "down": {"to": None, "emit": "down"},
                             "left": {"to": None, "emit": "left"},
                             "right": {"to": None, "emit": "right"},
                             "up_left": {"to": None, "emit": "up_left"},
                             "up_right": {"to": None, "emit": "up_right"},
                             "down_left": {"to": None, "emit": "down_left"},
                             "down_right": {"to": None, "emit": "down_right"}},
                },
                "min_confidence": 0,
                "fallback": None,
                "x": 40, "y": 120,
            },
        },
    },
    # ----------------------------------------------------------------- duelo
    # Duas cadeiras, dois cerebros. Cada funil declara a cadeira que joga; com o
    # driver em "auto", quem diz de quem e a vez e o jogo.
    "racha_azul": {
        "id": "racha_azul",
        "name": "Racha azul: atacante direto",
        "game": "street_football",
        "seat": "azul_1",
        "entry": "jogada",
        "nodes": {
            "jogada": {
                "label": "O lance do azul",
                # Um no so: le tudo de uma vez e responde. O "chutar agora" vem
                # antes do rumo, entao quando ele diz sim o rumo vira leitura.
                "questions": {
                    "chutar_agora": {
                        "type": "noul",
                        "label": "Chutar agora",
                        "instructions": (
                            "Responda alto so quando `pronto_para_chutar` for verdadeiro. "
                            "Ele ja soma as duas condicoes: ter a bola ao alcance e estar "
                            "DENTRO da pequena area (`na_area_do_gol`). Chute de fora da "
                            "area e anulado e entrega a bola ao adversario, entao nao "
                            "adianta tentar o chutao de longe."
                        ),
                        "threshold": 0.55,
                    },
                    "rumo": {
                        "type": "choice",
                        "label": "Para onde correr",
                        "instructions": (
                            "Este jogador e direto: quer chegar no gol. Se nao tem a bola "
                            "(`com_a_bola` falso), va buscar em `bola.direcao`. Se tem, leve "
                            "para `meu_gol.direcao` e siga ate estar dentro da area. "
                            "MAS os corpos se esbarram: voce NAO atravessa o adversario. Se `adversarios[0].encostando` for verdadeiro, ir reto e ficar preso nele; nesse caso escolha a diagonal que contorna, desviando pelo lado oposto ao `distancia_y` dele. " + 'Os campos `distancia_x` e `distancia_y` dizem quanto falta em cada eixo, com sinal: positivo e para a direita e para baixo. As diagonais quase sempre andam menos que a escada de dois comandos retos.'
                        ),
                        "criteria": {
                            "up": "So para cima: o alvo esta quase na mesma coluna, com y menor",
                            "down": "So para baixo: o alvo esta quase na mesma coluna, com y maior",
                            "left": "So para a esquerda: o alvo esta quase na mesma linha, com x menor",
                            "right": "So para a direita: o alvo esta quase na mesma linha, com x maior",
                            "up_left": "Diagonal: o alvo esta acima E a esquerda",
                            "up_right": "Diagonal: o alvo esta acima E a direita",
                            "down_left": "Diagonal: o alvo esta abaixo E a esquerda",
                            "down_right": "Diagonal: o alvo esta abaixo E a direita",
                        },
                    },
                },
                "outputs": {
                    "chutar_agora": {"sim": {"to": None, "emit": "chutar"},
                                     "nao": {"to": None, "emit": None}},
                    "rumo": {"up": {"to": None, "emit": "up"},
                             "down": {"to": None, "emit": "down"},
                             "left": {"to": None, "emit": "left"},
                             "right": {"to": None, "emit": "right"},
                             "up_left": {"to": None, "emit": "up_left"},
                             "up_right": {"to": None, "emit": "up_right"},
                             "down_left": {"to": None, "emit": "down_left"},
                             "down_right": {"to": None, "emit": "down_right"}},
                },
                "min_confidence": 0,
                "fallback": None,
                "x": 40, "y": 120,
            },
        },
    },
    "racha_laranja": {
        "id": "racha_laranja",
        "name": "Racha laranja: marcador paciente",
        "game": "street_football",
        "seat": "laranja_1",
        "entry": "posse",
        "nodes": {
            # Funil de varios saltos: primeiro le de quem e a bola, e so entao
            # decide — num no diferente para cada situacao. Custa uma chamada a
            # mais por jogada e em troca cada no faz uma pergunta so, mais nitida.
            "posse": {
                "label": "De quem e a bola",
                "questions": {
                    "posse": {
                        "type": "choice",
                        "label": "Quem esta com a bola",
                        "instructions": (
                            "Leia `com_a_bola`, `adversario_com_a_bola` e `bola_solta`. "
                            "Exatamente um dos tres e verdadeiro."
                        ),
                        "criteria": {
                            "minha": "`com_a_bola` verdadeiro: a bola e minha",
                            "dele": "`adversario_com_a_bola` verdadeiro: o adversario domina",
                            "solta": "`bola_solta` verdadeiro: ninguem domina a bola",
                        },
                    },
                },
                "outputs": {
                    "posse": {"minha": {"to": "atacar", "emit": None},
                              "dele": {"to": "marcar", "emit": None},
                              "solta": {"to": "disputar", "emit": None}},
                },
                "min_confidence": 0,
                "fallback": None,
                "x": 40, "y": 60,
            },
            "atacar": {
                "label": "Com a bola: chegar perto antes de chutar",
                "questions": {
                    "chutar_agora": {
                        "type": "noul",
                        "label": "Ja da para chutar",
                        "instructions": (
                            "Este jogador e paciente: nao chuta de longe. Responda alto so "
                            "quando `pronto_para_chutar` for verdadeiro — ele ja exige estar "
                            "DENTRO da pequena area (`na_area_do_gol`), que e de onde o gol "
                            "vale. De fora da area o chute e anulado; nesse caso leve a bola."
                        ),
                        "threshold": 0.6,
                    },
                    "rumo": {
                        "type": "choice",
                        "label": "Levando a bola",
                        "instructions": (
                            "Leve a bola para `meu_gol.direcao` ate entrar na area. "
                            "MAS os corpos se esbarram: voce NAO atravessa o adversario. Se `adversarios[0].encostando` for verdadeiro, ir reto e ficar preso nele; nesse caso escolha a diagonal que contorna, desviando pelo lado oposto ao `distancia_y` dele. "
                            + 'Os campos `distancia_x` e `distancia_y` dizem quanto falta em cada eixo, com sinal: positivo e para a direita e para baixo. As diagonais quase sempre andam menos que a escada de dois comandos retos.'
                        ),
                        "criteria": {
                            "up": "So para cima: o alvo esta quase na mesma coluna, com y menor",
                            "down": "So para baixo: o alvo esta quase na mesma coluna, com y maior",
                            "left": "So para a esquerda: o alvo esta quase na mesma linha, com x menor",
                            "right": "So para a direita: o alvo esta quase na mesma linha, com x maior",
                            "up_left": "Diagonal: o alvo esta acima E a esquerda",
                            "up_right": "Diagonal: o alvo esta acima E a direita",
                            "down_left": "Diagonal: o alvo esta abaixo E a esquerda",
                            "down_right": "Diagonal: o alvo esta abaixo E a direita",
                        },
                    },
                },
                "outputs": {
                    "chutar_agora": {"sim": {"to": None, "emit": "chutar"},
                                     "nao": {"to": None, "emit": None}},
                    "rumo": {"up": {"to": None, "emit": "up"},
                             "down": {"to": None, "emit": "down"},
                             "left": {"to": None, "emit": "left"},
                             "right": {"to": None, "emit": "right"},
                             "up_left": {"to": None, "emit": "up_left"},
                             "up_right": {"to": None, "emit": "up_right"},
                             "down_left": {"to": None, "emit": "down_left"},
                             "down_right": {"to": None, "emit": "down_right"}},
                },
                "min_confidence": 0,
                "fallback": None,
                "x": 360, "y": 40,
            },
            "marcar": {
                "label": "Sem a bola: marcar quem esta com ela",
                "questions": {
                    "rumo": {
                        "type": "choice",
                        "label": "Para cima do adversario",
                        "instructions": (
                            "O adversario esta com a bola. Va para cima dele: use "
                            "`adversarios[0].direcao`. Encostar nele e o que permite roubar. "
                            + 'Os campos `distancia_x` e `distancia_y` dizem quanto falta em cada eixo, com sinal: positivo e para a direita e para baixo. As diagonais quase sempre andam menos que a escada de dois comandos retos.'
                        ),
                        "criteria": {
                            "up": "So para cima: o alvo esta quase na mesma coluna, com y menor",
                            "down": "So para baixo: o alvo esta quase na mesma coluna, com y maior",
                            "left": "So para a esquerda: o alvo esta quase na mesma linha, com x menor",
                            "right": "So para a direita: o alvo esta quase na mesma linha, com x maior",
                            "up_left": "Diagonal: o alvo esta acima E a esquerda",
                            "up_right": "Diagonal: o alvo esta acima E a direita",
                            "down_left": "Diagonal: o alvo esta abaixo E a esquerda",
                            "down_right": "Diagonal: o alvo esta abaixo E a direita",
                        },
                    },
                },
                "outputs": {"rumo": {"up": {"to": None, "emit": "up"},
                             "down": {"to": None, "emit": "down"},
                             "left": {"to": None, "emit": "left"},
                             "right": {"to": None, "emit": "right"},
                             "up_left": {"to": None, "emit": "up_left"},
                             "up_right": {"to": None, "emit": "up_right"},
                             "down_left": {"to": None, "emit": "down_left"},
                             "down_right": {"to": None, "emit": "down_right"}}},
                "min_confidence": 0,
                "fallback": None,
                "x": 360, "y": 260,
            },
            "disputar": {
                "label": "Bola solta: correr nela",
                "questions": {
                    "rumo": {
                        "type": "choice",
                        "label": "Atras da bola",
                        "instructions": (
                            "A bola esta solta e quem chegar primeiro fica com ela. "
                            "Va em `bola.direcao`. " + 'Os campos `distancia_x` e `distancia_y` dizem quanto falta em cada eixo, com sinal: positivo e para a direita e para baixo. As diagonais quase sempre andam menos que a escada de dois comandos retos.'
                        ),
                        "criteria": {
                            "up": "So para cima: o alvo esta quase na mesma coluna, com y menor",
                            "down": "So para baixo: o alvo esta quase na mesma coluna, com y maior",
                            "left": "So para a esquerda: o alvo esta quase na mesma linha, com x menor",
                            "right": "So para a direita: o alvo esta quase na mesma linha, com x maior",
                            "up_left": "Diagonal: o alvo esta acima E a esquerda",
                            "up_right": "Diagonal: o alvo esta acima E a direita",
                            "down_left": "Diagonal: o alvo esta abaixo E a esquerda",
                            "down_right": "Diagonal: o alvo esta abaixo E a direita",
                        },
                    },
                },
                "outputs": {"rumo": {"up": {"to": None, "emit": "up"},
                             "down": {"to": None, "emit": "down"},
                             "left": {"to": None, "emit": "left"},
                             "right": {"to": None, "emit": "right"},
                             "up_left": {"to": None, "emit": "up_left"},
                             "up_right": {"to": None, "emit": "up_right"},
                             "down_left": {"to": None, "emit": "down_left"},
                             "down_right": {"to": None, "emit": "down_right"}}},
                "min_confidence": 0,
                "fallback": None,
                "x": 360, "y": 480,
            },
        },
    },
    "suporte_triagem": {
        "id": "suporte_triagem",
        "name": "Suporte: triagem de ticket",
        "game": "support",
        "entry": "departamento",
        "nodes": {
            "departamento": {
                "label": "Triagem do ticket",
                # tres perguntas num request so: uma ramifica, duas viram leitura
                "questions": {
                    "departamento": {
                        "type": "choice",
                        "label": "Departamento",
                        "instructions": "Qual equipe deve lidar com isso",
                        "criteria": {"pagamentos": "Problemas em pagamentos e inscricoes",
                                     "tecnico": "Bugs ou problemas de integracao",
                                     "vendas": "Questoes sobre precos ou assinaturas"},
                    },
                    "urgencia": {
                        "type": "noul",
                        "label": "Urgência",
                        "instructions": "A mensagem transmite urgencia ou sensibilidade ao tempo.",
                    },
                    "nivel_de_frustracao": {
                        "type": "score",
                        "label": "Frustração",
                        "instructions": "Quao frustrado o cliente esta",
                        "criteria": ["Calmo, so apresentando os fatos",
                                     "Frustrado, mas civilizado",
                                     "Muito agressivo, falando palavroes"],
                    },
                },
                "routes_by": "departamento",
                "outputs": {"pagamentos": {"to": None, "emit": "pagamentos"},
                            "tecnico": {"to": None, "emit": "tecnico"},
                            "vendas": {"to": None, "emit": "vendas"}},
                "x": 40, "y": 130,
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Normalizacao
# ---------------------------------------------------------------------------

def sanitize_funnel(raw):
    """Normaliza um funil para guardar e rodar.

    Aceita rascunho: funil sem no, no sem pergunta, escala pela metade. Quem
    reclama disso e o `validate_funnel`, para o editor sinalizar em vermelho;
    travar o salvamento so faria perder trabalho no meio do caminho.
    """
    if not isinstance(raw, dict):
        return None
    nodes_raw = raw.get("nodes")
    if not isinstance(nodes_raw, dict):
        nodes_raw = {}

    nodes = {}
    for node_id, node in list(nodes_raw.items())[:40]:
        nid = str(node_id).strip()[:60]
        if not nid or not isinstance(node, dict):
            continue
        node = _migrate_node(node)

        questions = {}
        brutas = node.get("questions") if isinstance(node.get("questions"), dict) else {}
        for qid, bruta in list(brutas.items())[:MAX_QUESTIONS_PER_NODE]:
            chave = str(qid).strip()[:60]
            limpa = _sanitize_question(bruta)
            if chave and limpa:
                questions[chave] = limpa

        # uma saida por resposta de cada pergunta; ligacoes existentes sobrevivem
        saidas_bruto = _migrate_outputs(node, questions)
        outputs = {}
        for qid, pergunta in questions.items():
            por_pergunta = saidas_bruto.get(qid) if isinstance(saidas_bruto.get(qid), dict) else {}
            outputs[qid] = {}
            for bid in branch_ids(pergunta):
                antiga = por_pergunta.get(bid) or {}
                outputs[qid][bid] = {
                    "to": (str(antiga.get("to")).strip()[:60] or None) if antiga.get("to") else None,
                    "emit": (str(antiga.get("emit")).strip()[:60] or None) if antiga.get("emit") else None,
                }

        try:
            floor = max(0.0, min(1.0, float(node.get("min_confidence") or 0)))
        except (TypeError, ValueError):
            floor = 0.0
        # a queda segura aponta para uma saida: {"question": qid, "branch": bid}
        fb = node.get("fallback")
        # formato antigo: so o nome da saida, da pergunta que ramificava
        if isinstance(fb, str) and fb:
            dono = node.get("routes_by") or next(iter(questions), None)
            fb = {"question": dono, "branch": fb} if dono else None
        fallback = None
        if isinstance(fb, dict):
            fq = str(fb.get("question") or "")[:60]
            fbid = str(fb.get("branch") or "")[:60]
            if fq in outputs and fbid in outputs[fq]:
                fallback = {"question": fq, "branch": fbid}
        try:
            x, y = int(float(node.get("x", 40))), int(float(node.get("y", 40)))
        except (TypeError, ValueError):
            x, y = 40, 40

        nodes[nid] = {
            "label": str(node.get("label") or nid).strip()[:80],
            "questions": questions,
            "outputs": outputs,
            # Piso de confianca: abaixo dele o no usa `fallback` em vez
            # do que o modelo escolheu. 0 desliga.
            "min_confidence": round(floor, 3),
            "fallback": fallback,
            "x": max(0, min(6000, x)),
            "y": max(0, min(6000, y)),
        }

    # Onde o usuario largou cada pilula de acao no canvas. Fica no funil, do
    # mesmo jeito que o x/y dos nos: e desenho do funil, nao preferencia de
    # quem esta olhando. Acao sem posicao aqui volta para a coluna padrao.
    posicoes = {}
    brutas = raw.get("action_pos")
    if isinstance(brutas, dict):
        for acao, ponto in list(brutas.items())[:60]:
            nome = str(acao).strip()[:60]
            if not nome or not isinstance(ponto, dict):
                continue
            try:
                px, py = int(float(ponto.get("x", 0))), int(float(ponto.get("y", 0)))
            except (TypeError, ValueError):
                continue
            posicoes[nome] = {"x": max(0, min(6000, px)), "y": max(0, min(6000, py))}

    entry = str(raw.get("entry") or "").strip()[:60]
    if nodes and entry not in nodes:
        entry = next(iter(nodes))
    elif not nodes:
        entry = ""
    fid = str(raw.get("id") or "").strip()[:60] or "funnel"
    return {
        "id": fid,
        "name": str(raw.get("name") or "").strip()[:80] or fid,
        "game": str(raw.get("game") or "").strip()[:60],
        # A cadeira que este funil joga. Vazio = funil de jogo inteiro, como
        # sempre foi. Preenchido = este cerebro joga esta vaga da partida.
        "seat": str(raw.get("seat") or "").strip()[:60],
        "entry": entry,
        "nodes": nodes,
        "action_pos": posicoes,
    }


# ---------------------------------------------------------------------------
# O que falta para rodar
# ---------------------------------------------------------------------------

def question_problems(qid, question):
    faltas = []
    rotulo = question.get("label") or qid
    if not (question.get("instructions") or "").strip():
        faltas.append(f"a pergunta '{rotulo}' esta sem enunciado")
    if question["type"] == "score" and len(question.get("criteria") or []) < 2:
        faltas.append(f"a escala de '{rotulo}' precisa de pelo menos 2 niveis")
    if question["type"] == "choice" and len(question.get("criteria") or {}) < 2:
        faltas.append(f"'{rotulo}' precisa de pelo menos 2 opcoes")
    return faltas


def ligacoes_do_no(node):
    """Todas as saidas ligadas: [(qid, branch, saida)]."""
    fora = []
    for qid, por_pergunta in (node.get("outputs") or {}).items():
        for bid, saida in (por_pergunta or {}).items():
            if saida.get("to") or saida.get("emit"):
                fora.append((qid, bid, saida))
    return fora


def node_problems(node, nid="", nodes=None, allowed_actions=()):
    """O que falta neste no para ele poder rodar. Lista vazia = pronto."""
    nodes = nodes if nodes is not None else {}
    rotulo = node.get("label", nid)
    questions = node.get("questions") or {}
    faltas = []

    if not questions:
        return [f"'{rotulo}' ainda nao tem pergunta nenhuma."]

    for qid, pergunta in questions.items():
        for falta in question_problems(qid, pergunta):
            faltas.append(f"'{rotulo}': {falta}.")

    ligadas = ligacoes_do_no(node)
    if not ligadas:
        faltas.append(f"'{rotulo}': nenhuma saida esta ligada, entao o no nao tem "
                      "como continuar. Arraste uma bolinha ate um no ou uma acao.")

    for qid, por_pergunta in (node.get("outputs") or {}).items():
        for bid, saida in (por_pergunta or {}).items():
            destino, emite = saida.get("to"), saida.get("emit")
            if destino and destino not in nodes:
                faltas.append(f"'{rotulo}' / {qid} → {bid} aponta para o no inexistente '{destino}'.")
            if emite and allowed_actions and emite not in allowed_actions:
                faltas.append(f"'{rotulo}' / {qid} → {bid} emite '{emite}', que nao esta "
                              f"nas acoes do jogo ({', '.join(allowed_actions)}).")

    if node.get("min_confidence") and not node.get("fallback"):
        faltas.append(f"'{rotulo}': tem piso de confianca mas nenhuma saida de seguranca escolhida.")
    return faltas


def validate_funnel(funnel, allowed_actions=()):
    """O que impede este funil de rodar. Lista vazia = pronto para pilotar."""
    problems = []
    nodes = funnel.get("nodes", {})
    entry = funnel.get("entry")
    if not nodes:
        return ["O funil esta vazio: crie o primeiro no."]
    if entry not in nodes:
        problems.append("Nenhum no esta marcado como inicio.")
    for nid, node in nodes.items():
        problems.extend(node_problems(node, nid, nodes, allowed_actions))

    reachable, stack = set(), [entry] if entry in nodes else []
    while stack:
        nid = stack.pop()
        if nid in reachable:
            continue
        reachable.add(nid)
        for por_pergunta in (nodes.get(nid, {}).get("outputs") or {}).values():
            for saida in (por_pergunta or {}).values():
                if saida.get("to") in nodes:
                    stack.append(saida["to"])
    for nid in nodes:
        if nid not in reachable:
            problems.append(f"'{nodes[nid].get('label', nid)}' nunca e alcancado a partir do inicio.")
    return problems


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------

def _read_stored():
    try:
        stored = json.loads(FUNNELS_FILE.read_text(encoding="utf-8"))
        return stored if isinstance(stored, dict) else {}
    except (OSError, ValueError):
        return {}


def load_funnels():
    """Embutidos, sobrescritos pelo que o editor salvou.

    Cada funil sai marcado com `builtin` (existe no codigo) e `saved` (existe
    no arquivo), para o editor saber se "restaurar" devolve o original ou
    apaga o funil de vez. Os embutidos passam pelo mesmo sanitize que os
    salvos, entao um erro na definicao aparece aqui e nao em producao.
    """
    merged = {}
    for fid, spec in DEFAULT_FUNNELS.items():
        limpo = sanitize_funnel(json.loads(json.dumps(spec)))
        if limpo:
            merged[fid] = limpo
    stored = _read_stored()
    for fid, raw in stored.items():
        limpo = sanitize_funnel(raw)
        if limpo:
            merged[str(fid)[:60]] = limpo
    for fid, spec in merged.items():
        spec["builtin"] = fid in DEFAULT_FUNNELS
        spec["saved"] = fid in stored
    return merged


def save_funnel(raw):
    """Guarda como esta, mesmo pela metade: a resposta leva os problemas junto."""
    clean = sanitize_funnel(raw)
    if not clean:
        raise ValueError("Nao consegui ler esse funil: esperava um objeto JSON.")
    stored = _read_stored()
    stored[clean["id"]] = clean
    FUNNELS_FILE.write_text(json.dumps(stored, indent=2, ensure_ascii=False), encoding="utf-8")
    return clean


def delete_funnel(funnel_id):
    """Remove um funil salvo. Um embutido volta ao original."""
    stored = _read_stored()
    existed = stored.pop(str(funnel_id), None) is not None
    FUNNELS_FILE.write_text(json.dumps(stored, indent=2, ensure_ascii=False), encoding="utf-8")
    return existed


# ---------------------------------------------------------------------------
# Execucao
# ---------------------------------------------------------------------------

def read_answer(qid, question, answer):
    """Normaliza uma resposta em {value, label, ...}, qualquer que seja o tipo."""
    tipo = question.get("type")
    out = {"id": qid, "type": tipo,
           "label_text": question.get("label") or qid,
           "confidence": answer.get("confidence")}
    if tipo == "noul":
        out["value"] = answer.get("noul")
        out["threshold"] = question.get("threshold", DEFAULT_THRESHOLD)
    elif tipo == "score":
        valor = answer.get("score")
        niveis = question.get("criteria") or []
        out["value"] = valor
        out["levels"] = niveis
        out["probabilities"] = answer.get("probabilities")
        if isinstance(valor, (int, float)) and niveis:
            # arredonda, nao trunca: 1.68 esta mais perto do nivel 2 que do 1
            i = max(0, min(len(niveis) - 1, int(round(valor))))
            legenda = answer.get("legend") or {}
            out["level"] = i
            out["label"] = legenda.get(str(i)) or niveis[i]
    else:
        out["value"] = answer.get("choice")
        out["label"] = answer.get("choice")
        out["probabilities"] = answer.get("probabilities")
    return out


def _branch_from(qid, question, answer):
    """Qual saida a resposta da pergunta que ramifica escolhe."""
    tipo = question.get("type")
    if tipo == "noul":
        valor = answer.get("noul")
        if not isinstance(valor, (int, float)):
            raise ValueError(f"A pergunta noul {qid!r} nao devolveu um numero: {answer!r}")
        return "sim" if valor >= question.get("threshold", DEFAULT_THRESHOLD) else "nao"
    if tipo == "score":
        valor = answer.get("score")
        niveis = question.get("criteria") or []
        if not isinstance(valor, (int, float)) or not niveis:
            raise ValueError(f"A pergunta score {qid!r} nao devolveu um numero: {answer!r}")
        return f"nivel_{max(0, min(len(niveis) - 1, int(round(valor))))}"
    return answer.get("choice")


def run_funnel(funnel, state, allowed_actions, call_jev, model):
    """Percorre o funil no Jev ate uma saida emitir uma acao do jogo.

    `call_jev(payload)` devolve `(data, latency_ms)`.
    Retorna `(action, trace, total_ms, usage, signals)`.
    """
    nodes = funnel.get("nodes", {})
    node_id = funnel.get("entry")
    nome = funnel.get("name", "?")
    if not nodes:
        raise ValueError(f"O funil '{nome}' esta vazio: abra o editor e crie o primeiro no.")
    if node_id not in nodes:
        raise ValueError(f"O funil '{nome}' nao tem no inicial marcado.")

    trace, total_ms, usage = [], 0.0, {"input_tokens": 0, "output_tokens": 0}
    signals, seen = {}, set()

    for _ in range(MAX_FUNNEL_HOPS):
        node = nodes.get(node_id)
        if not node:
            raise ValueError(f"No do funil nao encontrado: {node_id!r}")
        if node_id in seen:
            raise ValueError(f"Ciclo no funil: {node_id!r} foi visitado duas vezes.")
        seen.add(node_id)

        rotulo_no = node.get("label", node_id)
        questions = node.get("questions") or {}
        if not questions:
            raise ValueError(f"O no '{rotulo_no}' ainda nao tem pergunta nenhuma: "
                             "termine de monta-lo antes de pilotar.")
        outputs = node.get("outputs") or {}
        if not ligacoes_do_no(node):
            raise ValueError(f"O no '{rotulo_no}' nao tem nenhuma saida ligada: "
                             "ligue ao menos uma a um no ou a uma acao.")

        # Contexto do que ja foi decidido. Sem uma pergunta "dona" do no, vai
        # para todas: e informacao sobre o caminho, nao sobre a resposta.
        contexto = ""
        if trace:
            feito = "; ".join(f"{p['question']} = {p['choice']}" for p in trace)
            contexto = (f"\nDecisoes ja tomadas antes neste funil: {feito}. "
                        "Elas estao fechadas: escolha apenas entre as opcoes listadas agora, "
                        "sem contradize-las.")

        # TODAS as perguntas do no saem num request so.
        payload_questions = {}
        for qid, pergunta in questions.items():
            item = {"type": pergunta["type"],
                    "instructions": pergunta["instructions"] + contexto}
            if pergunta["type"] in ("choice", "score") and pergunta.get("criteria"):
                item["criteria"] = pergunta["criteria"]
            payload_questions[qid] = item

        data, latency_ms = call_jev({"state": state, "model": model,
                                     "questions": payload_questions})
        total_ms += latency_ms
        for chave in usage:
            try:
                usage[chave] += int(data.get("usage", {}).get(chave) or 0)
            except (TypeError, ValueError):
                pass

        respostas = data.get("answers", {})
        leituras = {qid: read_answer(qid, pergunta, respostas.get(qid) or {})
                    for qid, pergunta in questions.items()}

        # A primeira pergunta cuja resposta caiu numa saida ligada decide o rumo.
        # As demais sao leitura: nao acionam nada, so informam.
        decidiu = None
        for qid, pergunta in questions.items():
            bid = _branch_from(qid, pergunta, respostas.get(qid) or {})
            saida = (outputs.get(qid) or {}).get(bid) or {}
            if saida.get("to") or saida.get("emit"):
                decidiu = (qid, bid, saida)
                break
        if not decidiu:
            ligadas = ", ".join(f"{q} → {b}" for q, b, _ in ligacoes_do_no(node))
            raise ValueError(f"No no '{rotulo_no}' nenhuma resposta caiu numa saida ligada. "
                             f"Ligadas: {ligadas}.")

        qid_rota, choice, saida = decidiu
        confidence = (respostas.get(qid_rota) or {}).get("confidence")

        # Distribuicao quase plana quer dizer que o modelo nao tem opiniao. Pegar
        # o argmax assim mesmo e como um funil produz besteira com cara de decisao.
        guard = None
        floor, fb = node.get("min_confidence") or 0, node.get("fallback")
        if floor and isinstance(fb, dict) and isinstance(confidence, (int, float)) and confidence < floor:
            alvo = (outputs.get(fb["question"]) or {}).get(fb["branch"]) or {}
            if (alvo.get("to") or alvo.get("emit")) and (fb["question"], fb["branch"]) != (qid_rota, choice):
                guard = {"model_choice": f"{qid_rota} → {choice}",
                         "confidence": confidence, "threshold": floor}
                qid_rota, choice, saida = fb["question"], fb["branch"], alvo

        extras = {qid: leitura for qid, leitura in leituras.items() if qid != qid_rota}
        signals.update(extras)

        trace.append({
            "node": node_id,
            "label": rotulo_no,
            "question": qid_rota,
            "question_label": questions[qid_rota].get("label") or qid_rota,
            "type": questions[qid_rota]["type"],
            "choice": choice,
            "branch_label": branch_label(questions[qid_rota], choice),
            "reading": leituras.get(qid_rota),
            "confidence": confidence,
            "probabilities": (respostas.get(qid_rota) or {}).get("probabilities"),
            "guard": guard,
            "answers": leituras,
            "signals": extras,
            "latency_ms": latency_ms,
        })

        if saida.get("emit"):
            acao = saida["emit"]
            if allowed_actions and acao not in allowed_actions:
                raise ValueError(f"O funil emitiu {acao!r}, que nao esta entre as acoes "
                                 f"declaradas pelo jogo: {list(allowed_actions)}")
            return acao, trace, round(total_ms, 1), usage, signals
        node_id = saida["to"]

    raise ValueError(f"O funil passou de {MAX_FUNNEL_HOPS} saltos sem emitir uma acao.")
