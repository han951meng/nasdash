# nasdash 开发 / 发版工作流

基于 `v1.7.9` 基线。所有修改一律从干净基线出发，绝不用旧包 / 旧图标当母版。

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
git checkout v1.7.9        # 或 git pull 到最新 main（HEAD 即 1.7.9 发版 commit）
grep '^version' manifest   # 确认 version = 1.7.9
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
./release.py 1.7.10 "一句话更新要点"
```

自动改 `manifest`(version/desc/changelog) + `README.md`(版本号/更新日志) + 重建 fpk + 校验。
（手动方式见 `update_manifest.py` 旧脚本。）

## Step 5 · 重建 fpk + 一致性校验（一键）

```bash
./build.sh        # 内含 ./verify.sh
```

`verify.sh` 自动挡：三处 md5 一致、manifest desc 单行（否则应用中心报 10111）、版本号三处（manifest / fpk / README）一致。

## Step 6 · 部署到 NAS（版本号变更必须走重装）

```bash
# 新机 192.168.100.130，密码 hanyuvip；旧机 192.168.50.158 密码 Hanyuvip
scp nasdash.fpk admin@192.168.100.130:/tmp/
sshpass -p hanyuvip ssh -o StrictHostKeyChecking=no admin@192.168.100.130 '
  echo hanyuvip | sudo -S appcenter-cli stop com.dashboard.nasdash
  echo hanyuvip | sudo -S appcenter-cli uninstall com.dashboard.nasdash
  echo hanyuvip | sudo -S appcenter-cli install-fpk /tmp/nasdash.fpk --volume 1
  echo hanyuvip | sudo -S appcenter-cli start com.dashboard.nasdash
'
```

- 用户配置（`fan_disk_temp.json` / `fan_sys_temp.json` / `fan_labels.json` / `board_override.txt`）会在 `uninstall` 时自动备份到持久目录 `@appdata`，重装后由 `install_callback` 自动还原，**无需手动备份**。
- 首启若报 10500（端口竞态），再 `start` 一次自愈。

## Step 7 · 真机验证

```bash
appcenter-cli list                                  # 版本号与 manifest 一致
curl -s http://localhost:9800/api/version          # current == manifest
curl -s http://localhost:9800/api/all              # HTTP 200
ss -ltnp | grep 9800                               # 端口在监听
ps -o user= -p $(pgrep -f com.dashboard.nasdash)   # 运行用户为 root
```

## Step 8 · 发版

```bash
git add -A && git commit -m "release: v1.7.10"
git tag v1.7.10 && git push && git push --tags
gh release upload v1.7.10 nasdash.fpk --clobber
```

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
