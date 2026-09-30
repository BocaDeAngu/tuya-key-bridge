#!/usr/bin/env python3
"""boca_tuya_bridge — ponte NDJSON entre o cortemes e o SDK oficial da Tuya.

Extrai localKeys (e DPs, IP, status) dos devices de uma conta Smart Life/Tuya
via login por QR — sem conta de desenvolvedor Tuya, sem Access ID/Secret.

REIMPLEMENTAÇÃO INDEPENDENTE (MIT) sobre tuya-device-sharing-sdk (MIT, Tuya).
Não contém código do tuya-local-key (sem licença); a ideia do login por QR com
o registro público do app Home Assistant vem do ecossistema HA/LocalTuya.

Uso orquestrado (cortemes chama e lê stdout):

    python -m boca_tuya_bridge run --user-code X --qr-png out.png [opções]
    python -m boca_tuya_bridge status --device-id X --ip Y --dp 1   # key via stdin
    python -m boca_tuya_bridge set --device-id X --ip Y --dp 1 --ligar true

Saída: NDJSON — uma linha JSON por evento, sem texto solto:

    {"event":"iniciando"}
    {"event":"sessao_invalida"}                       # cache existia, era inválida
    {"event":"devices","devices":[{...}]}             # sessão em cache válida → direto
    {"event":"qr","png":"out.png"}                    # gerou QR — escanear no Smart Life
    {"event":"aguardando","restante":137}             # a cada poll (~2s) até confirmar
    {"event":"devices","devices":[{...}]}             # confirmado no app
    {"event":"erro","mensagem":"..."}                 # qualquer falha

Controle local (status/set): "status" também sai pós-comando em `set`:

    {"event":"status","ligada":true,"dps":{"1":true,...}}  # dps = estado real lido

A localKey do status/set vai por STDIN (JSON {"key":"..."}) — nunca na argv,
que fica visível no process list.

Códigos de saída: 0 ok · 2 uso inválido · 3 QR expirou · 1 erro.

Segurança: a sessão (tokens da conta) é gravada atomically com modo 600;
localKeys saem APENAS no evento devices do stdout — nada é logado nem salvo
em texto plano além do cache de sessão do próprio SDK.
"""

import argparse
import json
import os
import stat
import sys
import tempfile
import time
from types import SimpleNamespace

# Registro público do app device-sharing do Home Assistant (publicado no core
# do HA e do tuya-local — não é segredo; a autorização real é o scan do QR
# confirmado no SEU app).
CLIENT_ID = "HA_3y9q4ak7g4ephrvke"
SCHEMA = "haauthorize"

DEFAULT_SESSION = os.path.expanduser("~/.config/boca-tuya-bridge/session.json")
DEFAULT_QR_PNG = os.path.join(os.getcwd(), "tuya-bridge-qr.png")
POLL_SEGUNDOS = 2
DEFAULT_TIMEOUT = 150  # o QR do Tuya expira rápido (~1–2 min)

# Campos de token que persistimos na sessão (o SDK repõe via update_token).
TOKEN_FIELDS = ("t", "uid", "expire_time", "access_token", "refresh_token")

REQUEST_TIMEOUT_SECONDS = 60

# Controle local (tinytuya): tuyapi (Node, 2021) não fala protocolo 3.5 —
# tomadas novas ignoram o handshake dele; tinytuya 3.5 lê (prova 2026-10).
VERSAO_PADRAO = "3.5"
SOCKET_TIMEOUT_PADRAO = 5


def emitir(objeto):
    """NDJSON: uma linha JSON por evento, flush imediato (o leitor é outro processo).
    ensure_ascii=True: stdout 100% ASCII — imune ao cp1252 do Windows; o leitor
    (Node/JSON.parse) decodifica os escapes unicode de volta."""
    print(json.dumps(objeto, ensure_ascii=True), flush=True)


def sair(codigo):
    sys.stdout.flush()
    sys.exit(codigo)


# --------------------------------------------------------------------------- #
# Sessão (tokens da conta) — cache local, 600, escrita atômica
# --------------------------------------------------------------------------- #
def carregar_sessao(caminho):
    if not os.path.isfile(caminho):
        return None
    try:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def gravar_atomico(caminho, dados):
    """Escrita atômica (tmp + fsync + os.replace) com modo 600 — holds secrets."""
    caminho = os.fspath(caminho)
    pasta = os.path.dirname(caminho) or "."
    os.makedirs(pasta, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=pasta, prefix=".bridge-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(dados)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        os.replace(tmp, caminho)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class _SessaoSaver:
    """Duck-typed p/ o SDK: chamado a cada auto-refresh do token."""

    def __init__(self, caminho, sessao):
        self.caminho = caminho
        self.sessao = sessao

    def update_token(self, token_info):
        self.sessao["token_info"] = {k: token_info.get(k) for k in TOKEN_FIELDS}
        gravar_atomico(self.caminho, json.dumps(self.sessao, indent=2).encode("utf-8"))


# --------------------------------------------------------------------------- #
# Autenticação QR (SDK oficial)
# --------------------------------------------------------------------------- #
def mint_qr_token(user_code):
    """Pede à Tuya o token do QR de login. Lança RuntimeError em falha."""
    from tuya_sharing import LoginControl

    resp = LoginControl().qr_code(CLIENT_ID, SCHEMA, user_code)
    if not resp.get("success"):
        raise RuntimeError(f"falha ao iniciar login [{resp.get('code')}]: {resp.get('msg')}")
    return resp["result"]["qrcode"]


def poll_login(token, user_code):
    """Uma checagem NÃO-bloqueante: sessão se confirmado, None caso contrário."""
    from tuya_sharing import LoginControl

    try:
        ok, resultado = LoginControl().login_result(token, CLIENT_ID, user_code)
    except Exception:
        return None
    if not ok:
        return None
    return {
        "client_id": CLIENT_ID,
        "user_code": user_code,
        "terminal_id": resultado.get("terminal_id"),
        "endpoint": resultado.get("endpoint") or resultado.get("end_point"),
        "token_info": {k: resultado.get(k) for k in TOKEN_FIELDS},
    }


def gerar_qr_png(conteudo, caminho):
    """PNG do QR (qrcode[pil]) — o cortemes serve esse arquivo na página."""
    import qrcode

    qr = qrcode.QRCode(border=2)
    qr.add_data(conteudo)
    qr.make(fit=True)
    qr.make_image().save(caminho)


def _timeout_sdk():
    """Teto defensivo: se o SDK parar de ler esses globais, no-op silencioso."""
    try:
        import tuya_sharing.customerapi as customerapi
        import tuya_sharing.user as user

        customerapi.DEFAULT_TIMEOUT = user.DEFAULT_TIMEOUT = REQUEST_TIMEOUT_SECONDS
    except (ImportError, AttributeError):
        pass


def devices_da_sessao(sessao, caminho_sessao):
    """Lista de devices (CustomerDevice) a partir de uma sessão, via Manager."""
    from tuya_sharing import Manager

    _timeout_sdk()
    manager = Manager(
        sessao.get("client_id", CLIENT_ID),
        sessao["user_code"],
        sessao["terminal_id"],
        sessao["endpoint"],
        sessao["token_info"],
        _SessaoSaver(caminho_sessao, sessao),
    )
    manager.update_device_cache()  # renova token se necessário
    return list(manager.device_map.values())


# --------------------------------------------------------------------------- #
# Serialização dos devices (JSON-safe)
# --------------------------------------------------------------------------- #
def _plain(valor):
    """SimpleNamespace → dict; chaves int (local_strategy) → str; recursivo."""
    if isinstance(valor, SimpleNamespace):
        valor = vars(valor)
    if isinstance(valor, dict):
        return {str(k): _plain(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [_plain(v) for v in valor]
    return valor


def device_payload(device):
    """Campos que o cortemes usa: identidade, conectividade, key e DPs."""
    campos = ("name", "id", "uuid", "local_key", "product_id", "product_name",
              "category", "model", "ip", "online", "sub", "support_local")
    saida = {c: _plain(getattr(device, c, None)) for c in campos if hasattr(device, c)}
    saida["online"] = bool(saida.get("online"))
    for attr in ("status", "function", "status_range", "local_strategy"):
        valor = getattr(device, attr, None)
        if valor:
            saida[attr] = _plain(valor)
    return saida


# --------------------------------------------------------------------------- #
# Controle local (tinytuya, TCP 6668) — status e ligar/desligar
# --------------------------------------------------------------------------- #
def _device_local(device_id, ip, key, versao, timeout):
    import tinytuya

    if not ip:
        raise RuntimeError("IP da tomada na LAN é obrigatório para o controle local")
    return tinytuya.OutletDevice(device_id, ip, key, version=versao, connection_timeout=timeout)


def _dps_lidos(d):
    """Estado real (todos os DPs, metering incluído); chaves como str."""
    st = d.status() or {}
    return {str(k): v for k, v in (st.get("dps") or {}).items()}


def status_local(device_id, ip, key, dp, versao=VERSAO_PADRAO, timeout=SOCKET_TIMEOUT_PADRAO):
    d = _device_local(device_id, ip, key, versao, timeout)
    dps = _dps_lidos(d)
    return {"ligada": dps.get(str(dp)) is True, "dps": dps}


def comando_local(device_id, ip, key, dp, ligar, versao=VERSAO_PADRAO, timeout=SOCKET_TIMEOUT_PADRAO):
    d = _device_local(device_id, ip, key, versao, timeout)
    d.set_status(bool(ligar), dp)
    dps = _dps_lidos(d)  # reler: agir só depois de ler o estado real (regra 5 da 0331)
    return {"ligada": dps.get(str(dp)) is True, "dps": dps}


def _key_de_stdin(args):
    """localKey: --key existe para teste manual, mas o caminho normal é stdin
    (argv fica no process list/history; stdin não). stdin recebe JSON {\"key\":\"...\"}."""
    if args.key:
        return args.key
    linha = sys.stdin.readline().strip()
    if not linha:
        raise RuntimeError("localKey não fornecida (stdin JSON {\"key\":\"...\"} ou --key)")
    try:
        dados = json.loads(linha)
        key = dados.get("key") if isinstance(dados, dict) else None
    except json.JSONDecodeError:
        key = None
    if not key or not isinstance(key, str):
        raise RuntimeError('stdin deve ser JSON {"key":"..."}')
    return key


# --------------------------------------------------------------------------- #
# Fluxo principal
# --------------------------------------------------------------------------- #
def run(args):
    emitir({"event": "iniciando"})

    # 1. Sessão em cache válida → devices direto, sem QR (2ª execução em diante).
    if not args.relogin:
        sessao = carregar_sessao(args.session)
        if sessao:
            try:
                devices = devices_da_sessao(sessao, args.session)
                emitir({"event": "devices", "devices": [device_payload(d) for d in devices]})
                return 0
            except Exception as e:
                emitir({"event": "sessao_invalida", "motivo": str(e)})
                # cai para o QR

    # 2. Sem user code e sem sessão → erro de uso (o cortemes valida antes, mas
    #    o bridge defende: NUNCA input() interativo).
    if not args.user_code:
        emitir({"event": "erro", "mensagem": "user code obrigatório (Smart Life > Me > Account and Security > User Code)"})
        return 2

    # 3. Gera o QR de login e salva o PNG.
    try:
        token = mint_qr_token(args.user_code)
    except Exception as e:
        emitir({"event": "erro", "mensagem": f"falha ao gerar QR: {e} — confira o user code"})
        return 1
    try:
        gerar_qr_png(f"{args.qr_scheme}--qrLogin?token={token}", args.qr_png)
    except Exception as e:
        emitir({"event": "erro", "mensagem": f"falha ao gerar PNG do QR: {e}"})
        return 1
    emitir({"event": "qr", "png": os.path.abspath(args.qr_png)})

    # 4. Polla até o app confirmar (QR expira rápido — deadline no --timeout).
    limite = time.time() + args.timeout
    while time.time() < limite:
        sessao = poll_login(token, args.user_code)
        if sessao:
            break
        emitir({"event": "aguardando", "restante": int(limite - time.time())})
        time.sleep(POLL_SEGUNDOS)
    if not sessao:
        emitir({"event": "erro", "mensagem": f"login expirou ({args.timeout}s) — QR do Tuya expira rápido; reimporte com novo QR"})
        return 3

    # 5. Persiste a sessão (600) e busca os devices.
    gravar_atomico(args.session, json.dumps(sessao, indent=2).encode("utf-8"))
    try:
        devices = devices_da_sessao(sessao, args.session)
    except Exception as e:
        emitir({"event": "erro", "mensagem": f"logado, mas falha ao listar devices: {e}"})
        return 1
    emitir({"event": "devices", "devices": [device_payload(d) for d in devices]})
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="boca_tuya_bridge", description="Ponte NDJSON de localKeys Smart Life/Tuya (QR login, sem dev account)")
    p.add_argument("comando", choices=["run", "status", "set"], help="run=importar keys · status=ler tomada · set=ligar/desligar")
    p.add_argument("--user-code", help="User Code do Smart Life (Me > Account and Security)")
    p.add_argument("--device-id", help="[status/set] id do device")
    p.add_argument("--ip", help="[status/set] IP do device na LAN")
    p.add_argument("--key", help="[status/set] localKey (preferir stdin — argv fica no history/process list)")
    p.add_argument("--dp", type=int, default=1, help="[status/set] DP do relé (default 1)")
    p.add_argument("--ligar", choices=("true", "false"), help="[set] true=liga, false=desliga")
    p.add_argument("--versao", choices=("3.1", "3.3", "3.4", "3.5"), default=VERSAO_PADRAO, help="[status/set] protocolo do device")
    p.add_argument("--timeout", type=int, default=None, help="[run] s aguardando scan (150) · [status/set] timeout do socket em s (5)")
    p.add_argument("--qr-png", default=DEFAULT_QR_PNG, help="onde salvar o PNG do QR")
    p.add_argument("--session", default=DEFAULT_SESSION, help="cache da sessão (600)")
    p.add_argument("--qr-scheme", choices=["smartlife", "tuyaSmart"], default="smartlife", help="prefixo do QR (Smart Life lê ambos)")
    p.add_argument("--relogin", action="store_true", help="ignora a sessão em cache e pede novo QR")
    args = p.parse_args(argv)

    if args.comando in ("status", "set"):
        try:
            if not args.device_id:
                emitir({"event": "erro", "mensagem": "--device-id é obrigatório"})
                return 2
            key = _key_de_stdin(args)
            timeout = SOCKET_TIMEOUT_PADRAO if args.timeout is None else args.timeout
            if args.comando == "status":
                emitir({"event": "status", **status_local(args.device_id, args.ip, key, args.dp, args.versao, timeout)})
            else:
                if args.ligar is None:
                    emitir({"event": "erro", "mensagem": "--ligar true|false é obrigatório"})
                    return 2
                emitir({"event": "status", **comando_local(args.device_id, args.ip, key, args.dp, args.ligar == "true", args.versao, timeout)})
            return 0
        except Exception as e:
            emitir({"event": "erro", "mensagem": str(e)})
            return 1

    args.timeout = DEFAULT_TIMEOUT if args.timeout is None else args.timeout  # run: timeout do QR
    try:
        return run(args)
    except KeyboardInterrupt:
        emitir({"event": "erro", "mensagem": "interrompido"})
        return 130
    except Exception as e:  # rede fora, SDK mudou, etc — nunca traceback cru pro leitor
        emitir({"event": "erro", "mensagem": str(e)})
        return 1


if __name__ == "__main__":
    sair(main())
