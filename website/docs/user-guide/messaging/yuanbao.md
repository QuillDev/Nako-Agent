---
sidebar_position: 16
title: "Yuanbao"
description: "Yuanbao moved out of Nako core into the yuanbao Nako Labs plugin"
---

# Yuanbao

:::warning Nako Labs plugin
Yuanbao is now a Nako Labs plugin: built by Quill Dev, not supported, and likely not maintained going forward. Fixes from the community are welcome.
:::

The [Yuanbao](https://yuanbao.tencent.com/) gateway platform and its `yb_*` tools (`yb_query_group_info`, `yb_query_group_members`, `yb_send_dm`, `yb_search_sticker`, `yb_send_sticker`) are no longer part of Nako core. They ship as the `yuanbao` plugin in [NousResearch/nako-labs-plugins](https://github.com/NousResearch/nako-labs-plugins/tree/main/yuanbao), which carries the full setup guide.

## Install

```bash
nako plugins install yuanbao
```

Plugins are installed per profile; use `nako -p <profile> plugins install yuanbao` for another profile. Restart the gateway afterwards so it serves the platform.

Every name stays the same: the `YUANBAO_*` environment variables (`YUANBAO_APP_ID` or the legacy `YUANBAO_APP_KEY`, `YUANBAO_APP_SECRET`, `YUANBAO_HOME_CHANNEL`, ...), the `platforms.yuanbao` block (and the legacy `gateway.yuanbao` block) in `config.yaml`, the `yuanbao` toolset and the tool names.

:::info Upgrading from a release that bundled Yuanbao
Nothing to do. Every profile that was already using Yuanbao (`YUANBAO_APP_ID` or `YUANBAO_APP_KEY` plus `YUANBAO_APP_SECRET` in its `.env`, `platforms.yuanbao` enabled in `config.yaml`, or the `yuanbao` / `nako-yuanbao` toolset listed for another platform) gets the plugin installed automatically from the catalog by `nako update`, or the first time the profile starts (agent or gateway start; this honours `security.allow_lazy_installs`). A failed attempt is retried with a backoff; `nako update` always retries. This happens once per profile: if you later remove the plugin (`nako plugins remove yuanbao`), it stays removed.
:::
