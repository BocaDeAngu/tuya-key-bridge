# boca_tuya_bridge — ponte NDJSON de localKeys Smart Life/Tuya

**Reimplementação independente (MIT) sobre o [SDK oficial da Tuya](https://github.com/tuya/tuya-device-sharing-sdk) (MIT).**
Não é fork nem contém código do [vineetchoudhary/tuya-local-key](https://github.com/vineetchoudhary/tuya-local-key)
(sem licença); a ideia do login por QR com o registro público do app **Home Assistant**
(`HA_3y9q4ak7g4ephrvke` / `haauthorize`, publicada no core do HA e do tuya-local) vem do ecossistema HA/LocalTuya.

Sem conta de desenvolvedor Tuya, sem Access ID/Secret — o login é **seu** QR confirmado no **seu** app.

## Por que existe

O [cortemes](https://github.com/BocaDeAngu/cortemes) orquestra este processo como
dependência (`pip install git+https://github.com/BocaDeAngu/tuya-key-bridge@v0.1.0-boca`)
para importar localKeys direto na página de carregadores — sem terminal, sem CSV.

## Uso

```bash
python -m boca_tuya_bridge run --user-code X --qr-png out.png [--timeout 150] [--session caminho] [--relogin]
```

User Code: **Smart Life → Me → Account and Security → User Code**.

### Contrato de saída (NDJSON no stdout — uma linha JSON por evento)

| Evento | Quando |
|---|---|
| `{"event":"iniciando"}` | processo iniciou |
| `{"event":"devices","devices":[...]}` | sessão em cache válida → direto, sem QR |
| `{"event":"sessao_invalida","motivo":"..."}` | cache existia, era inválido → segue para QR |
| `{"event":"qr","png":"C:\\...\\out.png"}` | QR gerado — escanear no Smart Life (+ → Scan → Confirmar login) |
| `{"event":"aguardando","restante":137}` | a cada ~2s até o app confirmar |
| `{"event":"erro","mensagem":"..."}` | qualquer falha (exit ≠ 0) |

Códigos de saída: `0` ok · `2` uso inválido · `3` QR expirou · `1` erro.

Cada device vem com `name`, `id`, `local_key`, `ip`, `online`, `category`,
`product_name`, `model` e os DPs (`status`, `function`, `status_range`, `local_strategy`).

## Segurança

- A sessão (tokens da conta) é gravada **atomicamente com modo 600** em `~/.config/boca-tuya-bridge/session.json`; auto-refresh mantém o cache vivo — você só escaneia de novo quando o login expira de vez.
- **localKeys saem apenas no evento `devices` do stdout** — nada é logado nem gravado em texto claro além do cache de sessão.
- O QR do Tuya expira em ~1–2 min (`--timeout`, padrão 150s).
- Quem pode ler o stdout deste processo pode ler as keys — rode no servidor confiável, com o stdout consumido pelo cortemes (página SUPERUSER, permissão própria).

## Instalação

```bash
pip install git+https://github.com/BocaDeAngu/tuya-key-bridge@v0.1.0-boca
```

Requer Python 3.8+. Dependências: `tuya-device-sharing-sdk>=0.2.15` (MIT, Tuya) e `qrcode[pil]`.

## Créditos

- [tuya-device-sharing-sdk](https://github.com/tuya/tuya-device-sharing-sdk) — Tuya, MIT
- [vineetchoudhary/tuya-local-key](https://github.com/vineetchoudhary/tuya-local-key) — inspiração do fluxo QR (sem licença: não derivamos código)
- [Home Assistant Tuya integration](https://www.home-assistant.io/integrations/tuya/) / [tuya-local](https://github.com/make-all/tuya-local) — o registro público de device-sharing usado no login

MIT © BocaDeAngu — ver `LICENSE`.
