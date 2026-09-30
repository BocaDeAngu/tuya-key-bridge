# boca-tuya-bridge — Tuya / Smart Life localKeys without a developer account

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg) ![Python](https://img.shields.io/badge/python-3.8%2B-blue)

Get the **localKey** of your Tuya / Smart Life devices by scanning a QR with **your own app** — no developer account, no Access ID/Secret. Also control devices **locally over your LAN**, including **protocol 3.5** plugs that ignore older Node libraries (tuyapi).

Independent clean-room reimplementation (MIT) on top of Tuya's official [tuya-device-sharing-sdk](https://github.com/tuya/tuya-device-sharing-sdk) (MIT). Not a fork of [vineetchoudhary/tuya-local-key](https://github.com/vineetchoudhary/tuya-local-key) (unlicensed — no code derived); the QR-login idea with Home Assistant's public app registration (`HA_3y9q4ak7g4ephrvke` / `haauthorize`, published in the HA core and in tuya-local) comes from the HA/LocalTuya ecosystem.

## Quickstart

Requires Python 3.8+. With [pipx](https://pipx.pypa.io/) (recommended — isolated env, command on PATH):

```bash
pipx install git+https://github.com/BocaDeAngu/tuya-key-bridge.git
boca-tuya-bridge print --user-code X
```

(or `pip install git+https://github.com/BocaDeAngu/tuya-key-bridge.git`)

- **User Code**: Smart Life → Me → Account and Security → User Code.
- **First run**: the QR opens in your image viewer — scan it in the Smart Life app (Me → Scan → Confirm login). The session is cached afterwards, so the next runs list your devices instantly, no QR.
- The table shows `name · device id · ip · online · localKey`:

```text
1 device(s) in your account:
  Plasma1-tomada  eb7ea45d972fc88a4fekp4  ip 192.168.0.148  online  key XXXXXXXXXXXXXXXX
```

## Control locally (no cloud round-trip)

`status` / `set` talk straight to the device over your LAN (TCP 6668, [tinytuya](https://github.com/jasonacox/tinytuya), protocols 3.1–3.5). The key goes via **stdin**, never argv:

```bash
echo '{"key":"YOURKEY"}' | boca-tuya-bridge status --device-id ID --ip 192.168.1.50
echo '{"key":"YOURKEY"}' | boca-tuya-bridge set --device-id ID --ip 192.168.1.50 --ligar true
```

- `--ligar true|false` = on/off; `set` always re-reads the real state right after commanding.
- DP 1 is the typical relay (`--dp`); DPs 19/20/22 are metering (current/voltage/power).

> **Device ignores every handshake?** Newer plugs speak **protocol 3.5** — tinytuya handles it; older Node libraries (tuyapi ≤ 7.x, 2021) don't. That is exactly the problem this bridge was built around: use the subcommands above instead of tuyapi.

## Machine mode (NDJSON)

The `run` subcommand is the machine-to-machine interface — [cortemes](https://github.com/BocaDeAngu/cortemes) consumes it to import keys into its admin UI with zero terminal friction:

```bash
python -m boca_tuya_bridge run --user-code X --qr-png out.png [--timeout 150] [--session path] [--relogin]
```

One JSON event per line on stdout (event names/keys are the stable contract):

| Event | Meaning |
|---|---|
| `{"event":"iniciando"}` | process started |
| `{"event":"devices","devices":[...]}` | valid cached session → straight to the list, no QR |
| `{"event":"sessao_invalida","motivo":"..."}` | cache existed but was invalid → falls through to QR |
| `{"event":"qr","png":"C:\\...\\out.png"}` | QR generated — scan in Smart Life (Me → Scan → Confirm login) |
| `{"event":"aguardando","restante":137}` | every ~2s until the app confirms |
| `{"event":"erro","mensagem":"..."}` | any failure (exit ≠ 0) |

`status` / `set` answer a single event:

```json
{"event":"status","ligada":true,"dps":{"1":true,"20":1199,...}}
```

Exit codes: `0` ok · `2` usage · `3` QR expired · `1` error. Each device carries `name`, `id`, `local_key`, `ip`, `online`, `category`, `product_name`, `model` and its DPs (`status`, `function`, `status_range`, `local_strategy`).

## Security

- Account session tokens are written **atomically with mode 600** to `~/.config/boca-tuya-bridge/session.json`; auto-refresh keeps the cache alive — you only scan a new QR when the login expires for good.
- **localKeys only ever go to stdout** (the `devices` event / the `print` table) — never logged, never saved in plain text beyond the session cache.
- The login QR expires in ~1–2 min (`--timeout`, default 150s).
- Whoever can read this process's stdout can read your keys — run it on a trusted machine.

## Credits

- [tuya-device-sharing-sdk](https://github.com/tuya/tuya-device-sharing-sdk) — Tuya, MIT
- [tinytuya](https://github.com/jasonacox/tinytuya) — Jason Cox et al., MIT (local protocol 3.5)
- [vineetchoudhary/tuya-local-key](https://github.com/vineetchoudhary/tuya-local-key) — QR-flow inspiration (unlicensed: no code derived)
- [Home Assistant Tuya integration](https://www.home-assistant.io/integrations/tuya/) / [tuya-local](https://github.com/make-all/tuya-local) — the public device-sharing registration used for login

MIT © BocaDeAngu — see [LICENSE](LICENSE).
