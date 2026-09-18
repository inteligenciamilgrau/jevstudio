#!/usr/bin/env python3
"""Jev Framework — sistema 1 de 2.

Nao conhece nenhum jogo. Recebe um estado qualquer, roda um funil de perguntas
no Jev e devolve UMA das acoes que o proprio chamador declarou. Quem sabe o que
e "esquerda" ou "pagamentos" e o outro lado.

A chave da API mora aqui e so aqui: o sistema do jogo nunca a ve.

    python framework_server.py          # http://127.0.0.1:8000
"""
import json
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from http.client import RemoteDisconnected
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

import funnel

ROOT = Path(__file__).resolve().parent
SERVICE = "jev-framework"
VERSION = "1.0"
DEFAULT_JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_JEV_MODEL = "jev-latest"
DECISION_LOG_SIZE = 200
SESSION_FILE = ROOT / ".jev_session.json"


def _load_dotenv():
    """.env ao lado deste arquivo, ou na pasta acima. Env real sempre ganha."""
    for path in (ROOT / ".env", ROOT.parent / ".env"):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


def _get_api_key():
    return os.getenv("TYPESAFE_API_KEY", "").strip() or os.getenv("JEV_API_KEY", "").strip()


def _endpoint():
    return os.getenv("JEV_ENDPOINT", DEFAULT_JEV_ENDPOINT).strip() or DEFAULT_JEV_ENDPOINT


def _model():
    return os.getenv("JEV_MODEL", DEFAULT_JEV_MODEL).strip() or DEFAULT_JEV_MODEL


def _call_jev(endpoint, key, payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"{SERVICE}/{VERSION}",
        "Connection": "close",
    }
    timeout = float(os.getenv("JEV_TIMEOUT", "12"))
    attempts = max(1, int(os.getenv("JEV_RETRIES", "4")))
    started = time.perf_counter()
    last_error = None
    for attempt in range(attempts):
        req = Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            return data, round((time.perf_counter() - started) * 1000, 1)
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:
                detail = ""
            raise RuntimeError(f"TypeSafe HTTP {exc.code}: {detail or exc.reason}") from exc
        except (URLError, RemoteDisconnected, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"Falha ao falar com a TypeSafe apos {attempts} tentativa(s): {last_error}")


# --------------------------------------------------------------------------
# Registro do que passou por aqui, para o console mostrar ao vivo
# --------------------------------------------------------------------------

_decisions = deque(maxlen=DECISION_LOG_SIZE)
_peers = {}
_lock = threading.Lock()
_seq = 0


def _record(entry):
    global _seq
    with _lock:
        _seq += 1
        entry["seq"] = _seq
        entry["at"] = round(time.time(), 3)
        _decisions.append(entry)
        return _seq


def _decisions_since(since):
    with _lock:
        return [d for d in _decisions if d["seq"] > since]


def _touch_peer(agent, actions, remote):
    with _lock:
        peer = _peers.setdefault(agent, {"agent": agent, "first_seen": time.time(), "decisions": 0})
        peer.update({"actions": actions, "last_seen": time.time(), "address": remote})
        peer["decisions"] += 1


# --------------------------------------------------------------------------
# Conector
# --------------------------------------------------------------------------

def agent_decide(request, remote=""):
    state = request.get("state")
    if not isinstance(state, dict):
        raise ValueError("Campo 'state' obrigatorio e precisa ser um objeto.")
    raw_actions = request.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise ValueError("Campo 'actions' obrigatorio: liste as acoes que o jogo aceita.")
    actions = [str(a)[:60] for a in raw_actions[:24]]
    agent = str(request.get("agent") or "anonimo")[:60]

    requested = request.get("funnel")
    if isinstance(requested, dict):
        spec = funnel.sanitize_funnel(requested)
    else:
        spec = funnel.load_funnels().get(str(requested or ""))
    if spec is None:
        raise ValueError(f"Funil desconhecido: {requested!r}")

    key = _get_api_key()
    if not key:
        return {"action": None, "source": "no_key", "trace": [], "signals": {},
                "error": "Sem TYPESAFE_API_KEY no framework."}

    _touch_peer(agent, actions, remote)
    endpoint, model = _endpoint(), _model()
    try:
        action, trace, latency_ms, usage, signals = funnel.run_funnel(
            spec, state, actions, lambda payload: _call_jev(endpoint, key, payload), model
        )
    except Exception as exc:
        print(f"[{agent}] {exc}", file=sys.stderr)
        failure = {"action": None, "source": "error", "trace": [], "signals": {},
                   "agent": agent, "funnel": spec["id"], "error": str(exc)}
        _record(dict(failure))
        return failure

    result = {
        "action": action,
        "source": "jev",
        "agent": agent,
        "funnel": spec["id"],
        "trace": trace,
        "hops": len(trace),
        "signals": signals,
        "latency_ms": latency_ms,
        "usage": usage,
    }
    _record(dict(result))
    return result


# --------------------------------------------------------------------------
# Contrato do jogo: quais campos ele manda (entradas) e quais acoes aceita
# (saidas). E o que alguem precisa ter na frente para desenhar um funil.
# --------------------------------------------------------------------------

MAX_CONTRACT_FIELDS = 140


def _describe(value):
    if value is None:
        return "nulo", "null"
    if isinstance(value, bool):
        return "booleano", "true" if value else "false"
    if isinstance(value, (int, float)):
        return "numero", str(value)
    if isinstance(value, str):
        texto = value if len(value) <= 70 else value[:67] + "..."
        return "texto", texto
    return type(value).__name__, ""


def flatten_state(value, prefix="", out=None, depth=0):
    """Achata o estado em caminhos como `ticket.mensagem` ou `obstacles[0].lane`."""
    if out is None:
        out = []
    if len(out) >= MAX_CONTRACT_FIELDS or depth > 5:
        return out
    if isinstance(value, dict):
        if prefix:
            out.append({"path": prefix, "type": f"objeto ({len(value)} campos)",
                        "sample": ", ".join(list(value)[:6]), "container": True})
        for key, sub in value.items():
            flatten_state(sub, f"{prefix}.{key}" if prefix else str(key), out, depth + 1)
    elif isinstance(value, list):
        out.append({"path": prefix, "type": f"lista ({len(value)} itens)",
                    "sample": "vazia" if not value else "", "container": True})
        if value:
            flatten_state(value[0], f"{prefix}[0]", out, depth + 1)
    else:
        kind, sample = _describe(value)
        out.append({"path": prefix, "type": kind, "sample": sample, "container": False})
    return out


def ler_inputs(url):
    """Debug: pega o frame que o jogo entregaria, sem mexer no jogo.

    `/api/view` e o endpoint de observador: le sem avancar. `/api/contract`
    tambem nao avanca, mas entrega um estado de exemplo. `/api/frame` fica por
    ultimo porque em jogo com fila ele consome uma rodada — quando so sobra
    ele, a resposta avisa com `consumed_turn`.
    """
    base = str(url or "").strip().rstrip("/")
    if not base:
        raise ValueError("Informe a URL do jogo.")
    if not base.startswith(("http://", "https://")):
        base = "http://" + base

    erros = []
    for caminho, consome in [("/api/view", False), ("/api/contract", False), ("/api/frame", True)]:
        try:
            req = Request(base + caminho, method="GET",
                          headers={"User-Agent": f"{SERVICE}/{VERSION}", "X-Client": SERVICE})
            started = time.perf_counter()
            with urlopen(req, timeout=6) as response:
                cru = response.read()
            latency = round((time.perf_counter() - started) * 1000, 1)
            data = json.loads(cru.decode("utf-8"))
            state = data.get("state")
            if not isinstance(state, dict):
                erros.append(f"{caminho}: resposta sem 'state'")
                continue
            return {
                "ok": True,
                "url": base,
                "source": caminho,
                "consumed_turn": consome,
                "game": data.get("game"),
                "frame": data.get("frame"),
                "paused": data.get("paused"),
                "at": data.get("at"),
                "latency_ms": latency,
                "bytes": len(cru),
                "fields": flatten_state(state),
                "state": state,
                "actions": [a if isinstance(a, dict) else {"id": str(a), "label": str(a)}
                            for a in (data.get("actions") or [])],
                "stats": data.get("stats") or [],
            }
        except Exception as exc:
            erros.append(f"{caminho}: {type(exc).__name__}")
    return {"ok": False, "url": base,
            "error": "Nao consegui ler os inputs. " + "; ".join(erros)}


def fetch_contract(url):
    """Pergunta ao jogo o que ele aceita e o que ele manda.

    Usa /api/contract, que nao avanca nada. Se o jogo nao tiver esse endpoint,
    cai para /api/view; so em ultimo caso usa /api/frame, que pode consumir
    uma rodada em jogos com fila.
    """
    base = str(url or "").strip().rstrip("/")
    if not base:
        raise ValueError("Informe a URL do jogo.")
    if not base.startswith(("http://", "https://")):
        base = "http://" + base

    tentativas = [("/api/contract", False), ("/api/view", False), ("/api/frame", True)]
    erros = []
    for caminho, consome in tentativas:
        try:
            req = Request(base + caminho, method="GET",
                          headers={"User-Agent": f"{SERVICE}/{VERSION}", "X-Client": SERVICE})
            started = time.perf_counter()
            with urlopen(req, timeout=6) as response:
                data = json.loads(response.read().decode("utf-8"))
            latency = round((time.perf_counter() - started) * 1000, 1)
            state = data.get("state")
            actions = data.get("actions")
            if not isinstance(state, dict) or not isinstance(actions, list):
                erros.append(f"{caminho}: resposta sem 'state' ou 'actions'")
                continue
            # O contrato do endpoint cobre so o jogo carregado agora. O
            # handshake lista todos, e quem esta montando um funil para outro
            # agente precisa das acoes daquele agente, nao das do ativo.
            catalogo = []
            try:
                req = Request(base + "/api/handshake", method="GET",
                              headers={"User-Agent": f"{SERVICE}/{VERSION}"})
                with urlopen(req, timeout=4) as resposta:
                    hs = json.loads(resposta.read().decode("utf-8"))
                for jogo in hs.get("games", []):
                    if isinstance(jogo, dict) and jogo.get("id"):
                        catalogo.append({"id": jogo["id"], "name": jogo.get("name", jogo["id"]),
                                         "actions": list(jogo.get("actions") or [])})
            except Exception:
                pass

            return {
                "ok": True,
                "url": base,
                "source": caminho,
                "consumed_turn": consome,
                "game": data.get("game"),
                "games": catalogo,
                "latency_ms": latency,
                "actions": [a if isinstance(a, dict) else {"id": str(a), "label": str(a)}
                            for a in actions],
                "fields": flatten_state(state),
                "state": state,
            }
        except Exception as exc:
            erros.append(f"{caminho}: {type(exc).__name__}")
    return {"ok": False, "url": base, "error": "Nao consegui o contrato. " + "; ".join(erros)}


# --------------------------------------------------------------------------
# Driver: o framework como cliente. Puxa um frame, decide, devolve a acao.
# --------------------------------------------------------------------------

def _load_session():
    try:
        dados = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        return dados if isinstance(dados, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_session(dados):
    try:
        SESSION_FILE.write_text(json.dumps(dados, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


class Driver:
    """Pilota um jogo remoto que so sabe servir frames e receber acoes."""

    def __init__(self):
        sessao = _load_session()
        self.target = (sessao.get("target")
                       or os.getenv("GAME_URL", "http://127.0.0.1:8100")).rstrip("/")
        self.funnel_id = sessao.get("funnel") or "auto"
        try:
            self.hz = max(0.2, min(4.0, float(sessao.get("hz", 1.0))))
        except (TypeError, ValueError):
            self.hz = 1.0
        # `running` de proposito NAO e restaurado: subir o servidor nao pode
        # comecar a gastar chamadas de API sozinho.
        self.running = False
        self.retomado = bool(sessao)
        self.busy = False
        self.last = None
        self.error = None
        self.frames = 0
        threading.Thread(target=self._loop, daemon=True).start()

    # ---- conversa com o jogo ----
    def _get_frame(self):
        req = Request(f"{self.target}/api/frame", method="GET",
                      headers={"User-Agent": f"{SERVICE}/{VERSION}", "X-Client": SERVICE})
        with urlopen(req, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))

    def _send_action(self, action, frame_no, signals):
        body = json.dumps({"action": action, "by": SERVICE, "frame": frame_no,
                           "signals": signals}, ensure_ascii=False).encode("utf-8")
        req = Request(f"{self.target}/api/action", data=body, method="POST",
                      headers={"Content-Type": "application/json",
                               "User-Agent": f"{SERVICE}/{VERSION}", "X-Client": SERVICE})
        with urlopen(req, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))

    def _pick_funnel(self, game_id):
        funnels = funnel.load_funnels()
        if self.funnel_id and self.funnel_id != "auto":
            spec = funnels.get(self.funnel_id)
            if not spec:
                raise ValueError(f"Funil desconhecido: {self.funnel_id!r}")
            return spec
        for spec in funnels.values():
            if spec.get("game") == game_id:
                return spec
        raise ValueError(f"Nenhum funil declara game={game_id!r}. Escolha um na mao.")

    # ---- uma volta completa ----
    def step(self):
        if self.busy:
            return None
        self.busy = True
        started = time.perf_counter()
        try:
            frame = self._get_frame()
            actions = [a["id"] for a in frame.get("actions", [])]
            if not actions:
                raise ValueError("O frame nao trouxe nenhuma acao valida.")
            spec = self._pick_funnel(frame.get("game"))

            key = _get_api_key()
            if not key:
                raise ValueError("Sem TYPESAFE_API_KEY no framework.")
            endpoint, model = _endpoint(), _model()
            action, trace, latency_ms, usage, signals = funnel.run_funnel(
                spec, frame["state"], actions,
                lambda payload: _call_jev(endpoint, key, payload), model)

            ack = self._send_action(action, frame.get("frame"), signals)
            self.frames += 1
            self.error = None
            result = {
                "action": action, "source": "jev", "agent": frame.get("game"),
                "funnel": spec["id"], "trace": trace, "hops": len(trace),
                "signals": signals, "latency_ms": latency_ms, "usage": usage,
                "frame": frame.get("frame"), "stale": ack.get("stale"),
                "round_trip_ms": round((time.perf_counter() - started) * 1000, 1),
                "target": self.target,
            }
            self.last = result
            _record(dict(result))
            return result
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.running = False
            print(f"[driver] {self.error}", file=sys.stderr)
            _record({"action": None, "source": "error", "agent": "driver",
                     "funnel": self.funnel_id, "trace": [], "signals": {},
                     "error": self.error})
            return None
        finally:
            self.busy = False

    def _loop(self):
        while True:
            if self.running and not self.busy:
                self.step()
                time.sleep(max(0.0, 1.0 / max(0.2, self.hz)))
            else:
                time.sleep(0.05)

    def probe(self):
        """Confere se o alvo esta la e fala o protocolo de frames."""
        try:
            req = Request(f"{self.target}/api/handshake", method="GET",
                          headers={"User-Agent": f"{SERVICE}/{VERSION}"})
            started = time.perf_counter()
            with urlopen(req, timeout=5) as response:
                remote = json.loads(response.read().decode("utf-8"))
            return {"ok": True, "url": self.target, "remote": remote,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
        except Exception as exc:
            return {"ok": False, "url": self.target, "error": f"{type(exc).__name__}: {exc}"}

    def lembrar(self):
        _save_session({"target": self.target, "funnel": self.funnel_id, "hz": self.hz})

    def status(self):
        return {
            "target": self.target, "funnel": self.funnel_id, "hz": self.hz,
            "running": self.running, "busy": self.busy, "frames": self.frames,
            "last": self.last, "error": self.error,
        }


driver = Driver()


def handshake():
    """O que o outro sistema precisa saber para falar com este."""
    funnels = funnel.load_funnels()
    with _lock:
        peers = sorted(_peers.values(), key=lambda p: -p["last_seen"])
    return {
        "ok": True,
        "service": SERVICE,
        "version": VERSION,
        "jev": {"configured": bool(_get_api_key()), "endpoint": _endpoint(), "model": _model()},
        "accepts": {
            "endpoint": "POST /api/agent/decide",
            "body": {"agent": "<id do jogo>", "funnel": "<id do funil>",
                     "actions": ["<acao>", "..."], "state": {"...": "qualquer objeto"}},
            "returns": {"action": "<uma das actions>", "trace": "[...]", "signals": "{...}"},
        },
        "funnels": [
            {"id": f["id"], "name": f["name"], "game": f.get("game", ""),
             "nodes": len(f["nodes"]),
             "signals": sorted({sid for n in f["nodes"].values()
                                for sid in (n.get("signals") or {})})}
            for f in funnels.values()
        ],
        "driver": driver.status(),
        "peers": [{"agent": p["agent"], "actions": p.get("actions", []),
                   "decisions": p["decisions"],
                   "seconds_since_last": round(time.time() - p["last_seen"], 1)}
                  for p in peers],
    }


def probe_peer(url):
    """Aperto de mao na direcao contraria: o framework confere se ve o jogo."""
    base = str(url or "").strip().rstrip("/")
    if not base:
        raise ValueError("Informe a URL do outro sistema, ex.: http://127.0.0.1:8100")
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    target = base + "/api/handshake"
    started = time.perf_counter()
    try:
        req = Request(target, headers={"User-Agent": f"{SERVICE}/{VERSION}"}, method="GET")
        with urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
        return {"ok": True, "url": base, "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "remote": data}
    except Exception as exc:
        return {"ok": False, "url": base, "error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------


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

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_OPTIONS(self):
        permitida = origem_local(self.headers.get("Origin"))
        if not permitida:
            self.send_error(403); return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", permitida)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self.send_response(302); self.send_header("Location", "/arena.html"); self.end_headers(); return
        if path == "/api/handshake":
            self._json(200, handshake()); return
        if path == "/api/funnels":
            self._json(200, {"funnels": funnel.load_funnels()}); return
        if path == "/api/drive":
            self._json(200, driver.status()); return
        if path == "/api/contract":
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            alvo = driver.target
            for parte in query.split("&"):
                if parte.startswith("url="):
                    alvo = unquote(parte[4:]) or alvo
            try:
                self._json(200, fetch_contract(alvo))
            except Exception as exc:
                self._json(200, {"ok": False, "url": alvo, "error": str(exc)})
            return
        if path == "/api/decisions":
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            since = 0
            for part in query.split("&"):
                if part.startswith("since="):
                    try:
                        since = int(part[6:])
                    except ValueError:
                        since = 0
            self._json(200, {"decisions": _decisions_since(since), "seq": _seq}); return
        if not pode_servir(path):
            self.send_error(404); return
        super().do_GET()

    def do_POST(self):
        routes = ("/api/agent/decide", "/api/funnels", "/api/funnels/delete",
                  "/api/peers/probe", "/api/peers/inputs", "/api/drive")
        if self.path not in routes:
            self.send_error(404); return
        try:
            data = self._body()
            if self.path == "/api/agent/decide":
                self._json(200, agent_decide(data, self.client_address[0])); return
            if self.path == "/api/funnels":
                saved = funnel.save_funnel(data)
                self._json(200, {"ok": True, "funnel": saved,
                                 "problems": funnel.validate_funnel(saved)}); return
            if self.path == "/api/funnels/delete":
                funnel.delete_funnel(data.get("id"))
                self._json(200, {"ok": True, "funnels": funnel.load_funnels()}); return
            if self.path == "/api/peers/inputs":
                self._json(200, ler_inputs(data.get("url") or driver.target)); return
            if self.path == "/api/peers/probe":
                self._json(200, probe_peer(data.get("url"))); return
            if self.path == "/api/drive":
                cmd, value = data.get("cmd"), data.get("value")
                if cmd == "run":
                    driver.running = bool(value)
                    if driver.running:
                        driver.error = None
                elif cmd == "step":
                    threading.Thread(target=driver.step, daemon=True).start()
                elif cmd == "target":
                    base = str(value or "").strip().rstrip("/")
                    if base and not base.startswith(("http://", "https://")):
                        base = "http://" + base
                    driver.target = base or driver.target
                    driver.running = False
                    driver.lembrar()
                elif cmd == "funnel":
                    driver.funnel_id = str(value or "auto")
                    driver.lembrar()
                elif cmd == "hz":
                    driver.hz = max(0.2, min(4.0, float(value or 1)))
                    driver.lembrar()
                elif cmd == "probe":
                    self._json(200, {"ok": True, "probe": driver.probe(),
                                     **driver.status()}); return
                else:
                    raise ValueError(f"comando desconhecido: {cmd!r}")
                self._json(200, {"ok": True, **driver.status()}); return
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
    port = int(os.getenv("FRAMEWORK_PORT", os.getenv("PORT", "8000")))
    print("=" * 62)
    print(f"  JEV FRAMEWORK  ·  http://127.0.0.1:{port}")
    print("=" * 62)
    print(f"  funis carregados : {', '.join(funnel.load_funnels()) or 'nenhum'}")
    if _get_api_key():
        print(f"  Jev              : {_endpoint()} ({_model()})")
    else:
        print("  Jev              : SEM CHAVE — coloque TYPESAFE_API_KEY no .env")
    print(f"  handshake        : GET  http://127.0.0.1:{port}/api/handshake")
    print(f"  conector         : POST http://127.0.0.1:{port}/api/agent/decide")
    print(f"  driver           : puxa frames de {driver.target}"
          + (f", funil {driver.funnel_id}" if driver.funnel_id != "auto" else "")
          + ("  — retomado de onde parou" if driver.retomado else ""))
    print("  este processo nao conhece nenhum jogo.")
    print("=" * 62)
    QuietServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
