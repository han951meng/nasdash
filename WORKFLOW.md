# nasdash 开发 / 发版工作流

基于 `v1.8.7` 基线。所有修改一律从干净基线出发，绝不用旧包 / 旧图标当母版。

## Step 0 · 明确需求（先想清楚再动手）

- 要修的 bug：复现路径、期望行为、影响范围（哪些页面 / 接口 / 硬件）。
- 要加的功能：用户故事、配置项、UI 入口、是否要持久化配置。
- 不确定就先和用户确认，不要凭猜实现。

## Step 0.5 · 查飞牛官方开发者文档（避免绕弯路）

动手改代码 / 配置 / manifest / 图标前，先翻官方文档确认规范，少走弯路：

- 开发者文档总入口：<https://developer.fnnas.com/docs/>
- 应用图标规范：<https://developer.fnnas.com/docs/core-concepts/icon/>
  - 注意：官方写「ICON.PNG=64×64」是**误导**。实测只有位图内容全部 256×256 才清晰（参考 fnos-hermes-agent 做法）。图标统一由 `generate_icon.py` 生成。
- manifest / fpk 结构、应用中心生命周期（install / start / stop / uninstall）以官方文档为准。

## Step 1 · 基线对齐

```bash
git fetch
git checkout v1.8.7        # 或 git pull 到最新 main（HEAD 即 1.8.7 发版 commit）
grep '^version' manifest   # 确认 version = 1.8.7
```

## Step 2 · 编码 / 改图标

- 改 `app.py` / `ui` / `config` / `vendor`。
- 改图标**只改 `generate_icon.py`**（1024 母版下采样），重跑生成 4 个 256×256 文件，不要另起炉灶、不要按官方做真 64×64。

## Step 3 · 本地自测（不上真机，早暴露问题）

```bash
./local_smoke.sh
```

- `py_compile` 语法检查；若有 flask 则本地起服务 curl `/api/version`、`/api/all`。
- 硬件接口在 Mac 上可能 500，属正常，只要服务启动且 `/api/version` 返回 200 即可。

## Step 3.5 · 跑回归测试（守护历史 bug 不复发）

```bash
./test.sh          # 纯函数 pytest：品牌识别 / 阵列卡温度 / NVMe 通电时长 等 15 个用例
```

- 不依赖硬件，本地 venv 跑；覆盖 v1.7.5(通电时长逗号截断)、v1.7.8(金士顿误判三星) 等历史修复。
- 改了 `app.py` 的解析逻辑后务必跑一遍，再上真机。

## Step 4 · 改版本号 + 同步四处一致（一键）

```bash
./release.py 1.8.7 "一句话更新要点"
```

自动改 `manifest`(version/desc/changelog) + `README.md`(版本号/更新日志) + 重建 fpk + 校验。
（手动方式见 `update_manifest.py` 旧脚本。）

## Step 5 · 重建 fpk + 一致性校验（一键）

```bash
./build.sh        # 内含 ./verify.sh
```

`verify.sh` 自动挡：三处 md5 一致、manifest desc 单行（否则应用中心报 10111）、版本号三处（manifest / fpk / README）一致。

## Step 6 · 部署到 NAS（真机自测用无向导版）

测试/自测部署用**无向导版**（`bash build.sh` 产物，fpk 根不含 `wizard/`），经 trim-cli 一键部署。
（连接参数固定：`--host 192.168.50.158 --scheme ws --port 5666 --allow-insecure-ws`；须先 `export TRIM_CLI_SESSION_STORAGE=file`）

```bash
# 标准流：stop → uninstall → install-fpk → start
trim-cli ... app stop com.dashboard.nasdash --yes
trim-cli ... app uninstall com.dashboard.nasdash --yes
trim-cli ... app install-fpk /path/nasdash.fpk --volume-id 1 --yes
trim-cli ... app start com.dashboard.nasdash --yes
```

- **若 uninstall 被飞牛拦死**（NAS 上装的是向导/WebUI 版时常见）：改用 cp 兜底——先 `app stop`，再把本地 `app.tgz` 解包覆盖到运行目录 `/vol1/@appcenter/com.dashboard.nasdash/`，并同步 `/var/apps/<appid>/manifest` 版本号，最后 `app start`。
- 部署前先备份 `@appdata`：`sshpass -p hanyuvip ssh ... "tar czf /vol1/1000/nd_appdata_backup_$(date +%Y%m%d_%H%M%S).tar.gz -C /vol1/@appdata com.dashboard.nasdash"`。
- 首启若报 10500（端口竞态），再 `start` 一次自愈。

## Step 7 · 真机验证

```bash
appcenter-cli list                                  # 版本号与 manifest 一致
curl -s http://localhost:9800/api/version          # current == manifest
curl -s http://localhost:9800/api/all              # HTTP 200
ss -ltnp | grep 9800                               # 端口在监听
ps -o user= -p $(pgrep -f com.dashboard.nasdash)   # 运行用户为 root
```

## Step 8 · 发版（发布版必须带向导）

发布资产必须是**带向导完整版**（`bash build.sh --with-wizard` 产物，fpk 根含 `wizard/`），绝不能用无向导测试版当发布物。

```bash
# 1) 构建带向导发布版，并复制成发布资产名
bash build.sh --with-wizard
cp nasdash.fpk nasdash-release-vX.Y.Z.fpk

# 2) 提交源码 + 发布说明（fpk 本身是 Release 资产、不入库）
git add README.md app.py "docs/使用手册.md" manifest templates/index.html release_notes_vX.Y.Z.md
git commit -m "release: vX.Y.Z"
git tag vX.Y.Z && git push origin main && git push origin vX.Y.Z

# 3) 建 Release（发布说明作 -F，fpk 作位置参数放末尾）
gh release create vX.Y.Z -F release_notes_vX.Y.Z.md -t "nasdash vX.Y.Z" --latest nasdash-release-vX.Y.Z.fpk
```

- 发版后改了任何会进包的内容（手册/app.py/模板/配置），必须**重建带向导版 → `gh release delete-asset` 旧资产 → `gh release upload` 新资产**，并比对 GitHub 资产字节数 = 本地 fpk 字节数确认无误。
- 发布说明（`release_notes_*.md`）与操作手册面向最终用户，**禁止出现 build.sh / trim-cli / 无向导版 / 带向导版 / wizard / fnos-fpk-dev.md 等内部部署话术**。

## 附：回滚预案

- 部署前留底：`cp nasdash.fpk /tmp/nasdash_v<上一版>.fpk`。
- 出问题：新机 `uninstall → install-fpk /tmp/nasdash_v<上一版>.fpk --volume 1 → start`。
- git 角度：每个发版 commit 即回滚点，`git checkout v1.7.8` 重建即可。

## 附：大改动用分支

```bash
git checkout -b fix/xxx v1.7.8
# ... 开发验证 ...
git checkout main && git merge fix/xxx && git tag vX.Y.Z
```

## 关键坑（已踩过，勿再踩）

- `manifest` 的 `desc` / `changelog` 必须单行，换行 → 应用中心 10111。
- `manifest.checksum` 必须等于 `md5(app.tgz)`（GNU tar mtime 漂移，须重建时重算）。
- 随包二进制（如 storcli）必须塞进 `app.tgz` 内，fpk 顶层额外目录飞牛不解包。
- 图标位图全部 256×256，勿信官方「64×64」。
- `pkill -f com.dashboard.nasdash` 经 SSH 自匹配杀会话，只用 `appcenter-cli stop`。
