# Jev Studio

Uma bancada visual para montar **funis de decisão** com a API do
[Jev/TypeSafe](https://typesafe.ai) e ver esses funis jogando alguma coisa em
tempo real.

Você desenha o funil arrastando: cada nó faz perguntas ao Jev, e cada resposta
possível vira uma saída que você liga em outro nó ou numa ação do jogo. Enquanto
roda, os fios mostram a probabilidade de cada resposta em verde e vermelho, e dá
para ver exatamente por onde a decisão passou.

```
┌─────────────────────┐   pergunta         ┌──────────────────┐
│  framework  :8000   │ ─────────────────> │   API do Jev     │
│  (tem a chave)      │ <───────────────── │                  │
└─────────────────────┘   respostas        └──────────────────┘
     │            ↑
     │ GET        │ POST
     │ /api/frame │ /api/action
     ↓            │
┌─────────────────────┐
│  jogos      :8100   │   servidor passivo: não tem chave,
│  (sem chave)        │   não faz requisição para fora
└─────────────────────┘
```

São **dois processos separados**, de propósito. O servidor de jogos não conhece
o Jev, não tem chave de API e não fala com a internet — ele só entrega um estado
quando pedem e aplica uma ação quando mandam. Qualquer cliente pode se plugar.

---

## Rodando

Você precisa de **Python 3.9 ou mais novo** e de uma chave da TypeSafe. Não tem
dependência para instalar: os dois servidores usam só a biblioteca padrão.

### 1. A chave

```bash
cp .env.example .env      # no Windows: copy .env.example .env
```

Abra o `.env` e coloque a sua chave em `TYPESAFE_API_KEY`. O arquivo está no
`.gitignore` — ele não vai para o repositório.

### 2. Suba os dois

**Windows (PowerShell):**

```powershell
.\start_game.ps1        # porta 8100, abre o jogo no navegador
.\start_framework.ps1   # porta 8000, abre a bancada
```

Cada um numa janela. Os scripts acham o Python sozinhos, avisam se a chave
estiver faltando e abrem o navegador quando o servidor responde.

**Qualquer sistema:**

```bash
python game/game_server.py        # http://127.0.0.1:8100/game.html
python framework/framework_server.py   # http://127.0.0.1:8000/arena.html
```

> Use `127.0.0.1`, não `localhost`. No Windows o `localhost` tenta IPv6
> primeiro e pode somar 2 segundos em **cada** requisição.

### 3. Conecte

Na bancada, o painel **Sistemas conectados** já vem apontando para
`http://127.0.0.1:8100`. Clique em **handshake**: os dois se reconhecem e a
bancada lê o contrato do jogo — quais campos de estado ele manda e quais ações
aceita.

Aí é só abrir um funil e clicar em **▶ Pilotar**.

---

## A tela

**Esquerda** é o funil. Cada bloco é um nó; dentro dele, as perguntas; dentro de
cada pergunta, uma linha por resposta possível.

- **Arraste** de uma resposta até um nó ou uma pílula de ação para ligar.
- **Passe o mouse** num fio e clique no ✕ para desligar.
- Resposta sem ligação simplesmente não aciona nada — não é erro.
- Duas respostas diferentes podem levar à mesma ação. O destino ganha um selo
  com quantas chegam nele.

**Direita** é o monitor: o handshake, o piloto e as decisões ao vivo. O botão
**👁 ler inputs** mostra no log o estado cru que o jogo está mandando agora, sem
gastar chamada de API — serve para conferir o nome de um campo na hora de
escrever uma instrução.

---

## Como um funil funciona

Um nó é **um request só** para o Jev, com todas as perguntas dele juntas.
Quatro perguntas custam praticamente o mesmo que uma (~780ms).

Cada pergunta tem um tipo, e é o tipo que define quais saídas ela gera:

| tipo | o que o Jev devolve | saídas geradas |
|---|---|---|
| `choice` | uma opção + confiança + probabilidades | uma por opção do `criteria` |
| `noul` | um número de 0 a 1 (booleano probabilístico) | `sim` e `nao`, separados por um limiar |
| `score` | um número contínuo numa escala | uma por nível do `criteria` |
| `bounding_box` | uma região da imagem | (ainda não usado aqui) |

O `noul` não devolve campo de confiança — o próprio número já é a medida. E o
`score` é contínuo: `1.68` quer dizer entre o nível 1 e o 2, mais perto do 2.

Ao rodar, **a primeira resposta que caiu numa saída ligada decide o rumo**. As
outras viram leitura: aparecem na tela e no log, mas não acionam nada. Por isso
a ordem das perguntas importa.

### Piso de confiança

Nas opções avançadas de cada nó dá para exigir uma confiança mínima. Quando o
Jev responde abaixo dela — distribuição quase plana, sem opinião formada — o
funil descarta a escolha e vai para a saída de segurança que você definiu.
Pegar o argmax de uma distribuição plana é como um funil produz besteira com
cara de decisão.

---

## Os três jogos que vêm junto

| jogo | id | ações | para que serve |
|---|---|---|---|
| Vitamina | `vitamin_hunt` | `up` `down` `left` `right` | grade com paredes; tem editor de mapa, então dá para montar desafios repetíveis |
| Corrida | `race` | `accelerate` `brake` `left` `right` | tempo contínuo; tem modo passo a passo, em que o mundo só anda quando chega uma ação |
| Suporte | `support` | `pagamentos` `tecnico` `vendas` | um cliente reclamando; a tela mostra só o rosto e o texto, para a subjetividade aparecer |

---

## Escrevendo o seu próprio jogo

O jogo não precisa ser em Python nem morar neste repositório. Ele precisa
responder quatro rotas HTTP. O framework não supõe mais nada.

**`GET /api/handshake`** — quem é você

```json
{ "ok": true, "service": "meu-jogo", "version": "1.0",
  "games": [ { "id": "meu_jogo", "name": "Meu Jogo",
               "actions": ["pular", "agachar"] } ] }
```

**`GET /api/frame`** — o estado agora

```json
{ "frame": 12, "game": "meu_jogo", "paused": false,
  "state":   { "qualquer": "objeto JSON que descreva a partida" },
  "actions": [ { "id": "pular", "label": "⬆ Pular" } ] }
```

O `state` é livre. Ele vai inteiro para o Jev, e os nomes dos campos são o que
você cita nas instruções das perguntas. Nomes claros valem mais que nomes
curtos: `inimigo_mais_proximo.distancia` funciona melhor que `d`.

**`POST /api/action`** — aplique isto

```json
{ "action": "pular", "by": "jev-framework", "frame": 12 }
```

**`GET /api/contract`** — opcional, mas ajude quem for montar o funil

Mesma cara do `/api/frame`, com um estado de exemplo. A diferença é que ele
**não avança o jogo**. Sem essa rota o framework cai para `/api/view` e, em
último caso, para `/api/frame` — que em jogo de turno consome uma rodada.

Depois é só apontar a bancada para a URL do seu jogo e apertar **handshake**.

---

## Estrutura

```
framework/
  framework_server.py   servidor da bancada; é o único que tem a chave
  funnel.py             o motor: normaliza, valida e executa os funis
  arena.html            a bancada inteira, sem build e sem dependência
game/
  game_server.py        servidor de jogos, passivo
  game.html             visualizador, cliente manual e editor de mapa
jev_env.ps1             acha o Python e lê o .env (usado pelos outros scripts)
start_framework.ps1     sobe só a bancada
start_game.ps1          sobe só os jogos
start.ps1               sobe os dois
```

As páginas são HTML com `<script>` embutido — sem bundler, sem `npm install`.
Editar e dar F5 é o ciclo inteiro.

---

## Segurança

- A chave fica **só** no processo do framework. O servidor de jogos não tem
  chave e nenhum cliente HTTP de saída.
- Nenhuma rota devolve a chave; o handshake só informa `configured: true`.
- Os dois servidores escutam em `127.0.0.1` — não ficam expostos na rede.
- Arquivos que começam com ponto (`.env`, `.jev_*.json`) nunca são servidos, e
  só extensões de página e mídia saem pela porta. O código-fonte não é servido.
- O CORS aceita só origens da própria máquina. Sem isso, qualquer site aberto
  no seu navegador conseguiria ler estas rotas e disparar POST — inclusive
  gastando a sua cota de API.

Ainda assim, **isto é uma bancada de desenvolvimento**, não um serviço. Não
coloque atrás de um proxy público sem antes pensar em autenticação: o
framework aceita `POST /api/agent/decide` de qualquer cliente local e busca
qualquer URL que você mandar em `/api/peers/inputs`.

---

## Contribuindo

Ideias de jogo são muito bem-vindas — o projeto existe para testar em que tipo
de decisão subjetiva o Jev se sai bem e em qual ele se perde. Veja o
[CONTRIBUTING.md](CONTRIBUTING.md).

---

## Licença

MIT. Veja [LICENSE](LICENSE).
