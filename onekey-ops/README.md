# OneKey Ops（合并版一键安装/管理工具）

把你现有的两个脚本合并成一个可安装、可配置、可管理的工具：

- **origin-monitor**：直连源站探测（curl --resolve）+ 连续失败计数 + 报警（支持 ssh 中转或 Telegram 直发）
- **cn-check**：国内直连/代理探测 + 输出 JSON + 可选 scp 推送到国外服务器

> 说明：仓库里只放“模板配置”，**真正的密钥/代理 API/Token 建议只放到服务器本地**（/etc/onekey-ops/opsctl.env），不要提交到 GitHub。

---

## 目录约定（安装后）

- 程序目录：`/opt/onekey-ops`
- 配置目录：`/etc/onekey-ops`
  - `opsctl.env`（主配置）
  - `origin_targets.conf`（源站探测目标列表）
  - `domains.txt`（国内检测域名列表）
- 日志目录：`/var/log/onekey-ops`
- 状态目录：`/var/lib/onekey-ops`

---

## 一键安装（推荐：git clone 方式）

```bash
git clone <你的GitHub仓库地址>
cd onekey-ops
sudo bash install.sh
```

如果你希望安装完立刻启用 systemd 定时（不推荐，除非你已经改好了配置）：

```bash
sudo bash install.sh --enable-timers
```

---

## 配置（必做）

```bash
sudo nano /etc/onekey-ops/opsctl.env
```

常用配置项：

- `ORIGIN_ALERT_METHOD=none|ssh|telegram`
- `ORIGIN_ALERT_HOST / ORIGIN_ALERT_CMD / ORIGIN_SSH_KEY`（ssh 中转报警时）
- `TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID`（telegram 直发时）
- `CN_PROXY_API`（代理 API，建议只放本地）
- `CN_PUSH_ENABLE=0|1` + `CN_PUSH_*`（是否推送 result_cn.json）

目标/域名列表：

```bash
sudo nano /etc/onekey-ops/origin_targets.conf
sudo nano /etc/onekey-ops/domains.txt
```

---

## 使用方法

### 手动运行

```bash
opsctl origin run
opsctl cn run
```

### 查看状态

```bash
opsctl status
```

### 查看日志

```bash
opsctl logs origin --tail 200
opsctl logs cn --tail 200
```

---

## systemd 定时任务（可选）

安装脚本会把 unit 文件放到：`/etc/systemd/system/`

启用：

```bash
sudo systemctl enable --now onekey-ops-origin-monitor.timer
sudo systemctl enable --now onekey-ops-cn-check.timer
```

查看：

```bash
systemctl status onekey-ops-origin-monitor.timer
systemctl status onekey-ops-cn-check.timer
journalctl -u onekey-ops-origin-monitor.service -n 50 --no-pager
```

---

## targets.conf 格式（origin-monitor）

文件：`/etc/onekey-ops/origin_targets.conf`

每行：

```
name|domain|origin_ip|port|path|slow_time|scheme
```

- `port/path/slow_time/scheme` 可以省略，省略时走 `opsctl.env` 里的默认值。

---

## 安全建议（重要）

- 不要把 `CN_PROXY_API`、Telegram Token、SSH Key 等敏感信息提交到 GitHub。
- 建议：
  - `/etc/onekey-ops/opsctl.env` 权限设为 `600`
  - SSH key 使用专用 key，且只允许执行必要的命令（可配合 `authorized_keys` 的 `command=` 限制）

