# Contribuindo

Obrigado por aparecer. O projeto é uma bancada para descobrir **em que tipo de
decisão subjetiva o Jev se sai bem e em qual ele se perde** — então ideia de
jogo novo vale tanto quanto código.

## Antes de tudo

```bash
cp .env.example .env     # ponha a sua chave
python game/game_server.py
python framework/framework_server.py
```

Abra `http://127.0.0.1:8000/arena.html`, aperte **handshake** e **▶ Pilotar**.
Se isso funcionou, o ambiente está pronto. Não tem instalação, venv nem build.

## O que mais ajuda

**Jogos novos.** É a contribuição mais útil. Um jogo é um `state` em JSON e uma
lista de ações — veja "Escrevendo o seu próprio jogo" no README. Os bons casos
são os em que a decisão certa não sai de uma regra: triagem, prioridade, tom de
voz, risco. Jogo que se resolve com um `if` não ensina nada sobre o Jev.

**Funis de exemplo.** Se você montou um funil que se sai bem (ou que falha de
um jeito interessante), mande. Eles ficam em `framework/funnel.py`, na tabela
de embutidos.

**Relatos de onde o Jev erra.** Abra uma issue com o estado que você mandou, as
perguntas do nó e o que ele respondeu. Isso é dado, não reclamação.

## Regras da casa

**Nada de dependência nova sem conversar antes.** Hoje dá para clonar e rodar
em dez segundos, sem instalar nada. Esse custo baixo é o que faz alguém
experimentar. Se a sua mudança precisa de um pacote, abra uma issue explicando
por quê.

**Sem build no front.** `arena.html` e `game.html` são HTML com `<script>`
dentro. Editar e dar F5 é o ciclo inteiro. Sem bundler, sem framework.

**A chave nunca sai do framework.** O servidor de jogos não tem chave e não faz
requisição para fora. Se uma mudança sua precisar furar isso, é sinal de que a
responsabilidade está no lugar errado.

**Não comite `.env` nem `.jev_*.json`.** O `.gitignore` cuida disso, mas dê uma
olhada no `git status` antes do commit.

## Testando

Não existe suíte no repositório ainda — os testes foram escritos fora dele
durante o desenvolvimento. Se você for mexer em algo delicado, vale saber o que
costuma quebrar:

- **O editor carrega, mas a tela quebra no clique.** Só abrir a página não
  prova nada: boa parte do código só roda quando você seleciona um nó. Teste
  clicando em cada nó de cada funil.
- **Gestos de mouse.** Arrastar uma saída, soltar no vazio, clicar sem arrastar,
  arrastar uma ação do contrato. Cada um tem um caminho diferente no código.
- **Formato das saídas.** Elas são aninhadas por pergunta:
  `outputs[pergunta][resposta] = {to, emit}`. Já existiu um formato plano, e
  código que ainda espera o antigo falha em silêncio, sem erro no console.
- **Passo grande no jogo de corrida.** A colisão é por cruzamento, não por
  posição. Testes com passo pequeno não pegam o caso em que o obstáculo pula
  por cima do jogador.

Mandar teste junto é muito bem-vindo — inclusive para decidirmos onde eles
deveriam morar.

## Pull requests

- Um assunto por PR.
- Descreva o que você viu acontecendo, não só o que mudou.
- Comentário em código explica **por quê**, não o quê. O código já diz o quê.
- Português ou inglês, tanto faz.
