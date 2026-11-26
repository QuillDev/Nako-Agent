<p align="center">
  <img src="assets/banner.png" alt="nako agent" width="100%">
</p>

<p align="center">
  <a href="https://nako.bot"><img src="https://img.shields.io/badge/nako.bot-ec4899?style=flat-square" alt="nako.bot"></a>
  <a href="https://portal.nakode.moe/docs/"><img src="https://img.shields.io/badge/docs-18181c?style=flat-square" alt="docs"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-mit-18181c?style=flat-square" alt="license: mit"></a>
</p>

<h3 align="center">hi, i'm nako. i'm an agent that lives on your own machine.</h3>

<p align="center">
  i remember what you tell me, write down what i learn, and answer wherever you chat.
</p>

---

## install

**macos, linux, wsl**

```bash
curl -fsSL https://nako.bot/install.sh | bash
```

**windows (powershell)**

```powershell
iex (irm https://nako.bot/install.ps1)
```

then:

```bash
nako setup        # pick a model and set things up
nako              # start chatting in the terminal
nako gateway      # answer on discord, slack, telegram and more
nako doctor       # check that everything works
```

full guides live in the [docs](https://portal.nakode.moe/docs/).

## what she does

| | |
| --- | --- |
| **remembers** | keeps notes about you and your setup, and searches her own past conversations. |
| **learns** | writes a skill after a hard task and improves it the next time she uses it. |
| **answers anywhere** | terminal, desktop app, discord, slack, telegram, whatsapp, signal: one agent behind all of them. |
| **works unattended** | scheduled jobs in plain language, with results sent wherever you want them. |
| **uses any model** | switch provider or model with `nako model`. no lock-in. |
| **runs where you put her** | your laptop, a home server, a container or a remote box over ssh. |

## what this fork adds

nako agent is a patched fork of [hermes agent](https://github.com/NousResearch/hermes-agent). on top of upstream it carries:

- **nako portal**: her own chat app for web, desktop and android, bundled in.
- **agent secrets**: she can use a password or key without it ever appearing in the chat.
- **bitwarden sign-in**: unlocks the vault headlessly and signs in with passkeys.
- **model controls**: pick the model, reasoning effort and speed per chat.
- **more ways to bring a model**: antigravity and claude subscription support.
- **slack multiplayer**: behaves well in busy shared channels.

## the rest of the suite

| | |
| --- | --- |
| [nako portal](https://github.com/QuillDev/nako-portal) | her own chat app for web, desktop and android, signed in with a passkey. |
| [nanime](https://github.com/QuillDev/Nanime) | a self-hosted anime library and streaming app that plays on any device. |
| [nanotes](https://github.com/QuillDev/nanotes) | a floating markdown scratchpad that saves plain files. |
| [nakode](https://github.com/QuillDev/nakode) | a terminal layer for running agent sessions. experimental. |

## how the fork is built

upstream moves fast, so this repo is generated instead of hand-merged. the [`nako-agent`](https://github.com/QuillDev/nako-agent/tree/nako-agent) branch holds a pinned upstream commit, a short stack of patch files, and the script that renames everything. `main` is rebuilt from those on every update. to change something, change a patch there; never edit `main` by hand.

## credits and license

built on [hermes agent](https://github.com/NousResearch/hermes-agent) by nous research. thank you to everyone who works on it.

mit. see [license](LICENSE).
