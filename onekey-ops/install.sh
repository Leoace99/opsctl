#!/usr/bin/env bash
set -euo pipefail

# OneKey Ops installer
#
# 用法：
#   sudo bash install.sh
#   sudo bash install.sh --enable-timers
#   sudo bash install.sh --prefix /opt/onekey-ops

PREFIX="/opt/onekey-ops"
CONFIG_DIR="/etc/onekey-ops"
LOG_DIR="/var/log/onekey-ops"
STATE_DIR="/var/lib/onekey-ops"
BIN_LINK="/usr/local/bin/opsctl"
ENABLE_TIMERS=0
NO_SYSTEMD=0

usage() {
  cat <<'EOF'
OneKey Ops 安装脚本

用法：
  sudo bash install.sh [--prefix /opt/onekey-ops] [--enable-timers] [--no-systemd]

参数：
  --prefix PATH        安装目录（默认 /opt/onekey-ops）
  --enable-timers      安装后立即启用并启动 systemd timers（默认不启用）
  --no-systemd         不安装 systemd unit 文件（无 systemd 的系统可用）
EOF
}

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "请使用 root（或 sudo）运行：sudo bash install.sh" >&2
  exit 1
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix)
      PREFIX="$2"; shift 2 ;;
    --enable-timers)
      ENABLE_TIMERS=1; shift 1 ;;
    --no-systemd)
      NO_SYSTEMD=1; shift 1 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "未知参数：$1" >&2
      usage
      exit 1
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

need_cmd() {
  local c="$1"
  if ! command -v "$c" >/dev/null 2>&1; then
    return 1
  fi
  return 0
}

install_pkgs_apt() {
  apt-get update -y
  apt-get install -y "$@"
}

install_pkgs_yum() {
  if command -v dnf >/dev/null 2>&1; then
    dnf install -y "$@"
  else
    yum install -y "$@"
  fi
}

ensure_deps() {
  local missing=()
  for c in python3 curl ssh scp; do
    if ! need_cmd "$c"; then
      missing+=("$c")
    fi
  done

  if [[ ${#missing[@]} -eq 0 ]]; then
    return 0
  fi

  echo "检测到缺少命令：${missing[*]}，尝试自动安装..."

  if command -v apt-get >/dev/null 2>&1; then
    install_pkgs_apt python3 python3-venv python3-pip curl openssh-client
  elif command -v yum >/dev/null 2>&1 || command -v dnf >/dev/null 2>&1; then
    install_pkgs_yum python3 python3-pip curl openssh-clients
    # 某些发行版 venv 在 python3-venv
    if ! python3 -c "import venv" >/dev/null 2>&1; then
      install_pkgs_yum python3-venv || true
    fi
  else
    echo "无法识别包管理器，请手动安装：python3、python3-venv、pip、curl、openssh-client" >&2
    exit 1
  fi
}

ensure_deps

if [[ ! -f "$SCRIPT_DIR/opsctl.py" ]]; then
  echo "未找到 opsctl.py（请在仓库目录中运行 install.sh，或把完整仓库拷贝到服务器）" >&2
  exit 1
fi

echo "==> 安装到：$PREFIX"
mkdir -p "$PREFIX"

# 拷贝代码
install -m 0755 "$SCRIPT_DIR/opsctl.py" "$PREFIX/opsctl.py"
install -m 0644 "$SCRIPT_DIR/requirements.txt" "$PREFIX/requirements.txt"

mkdir -p "$PREFIX/config" "$PREFIX/systemd"
cp -f "$SCRIPT_DIR/config/opsctl.env.example" "$PREFIX/config/opsctl.env.example"
cp -f "$SCRIPT_DIR/config/origin_targets.conf.example" "$PREFIX/config/origin_targets.conf.example"
cp -f "$SCRIPT_DIR/config/domains.txt.example" "$PREFIX/config/domains.txt.example"
cp -f "$SCRIPT_DIR/systemd/onekey-ops-origin-monitor.service" "$PREFIX/systemd/onekey-ops-origin-monitor.service"
cp -f "$SCRIPT_DIR/systemd/onekey-ops-origin-monitor.timer" "$PREFIX/systemd/onekey-ops-origin-monitor.timer"
cp -f "$SCRIPT_DIR/systemd/onekey-ops-cn-check.service" "$PREFIX/systemd/onekey-ops-cn-check.service"
cp -f "$SCRIPT_DIR/systemd/onekey-ops-cn-check.timer" "$PREFIX/systemd/onekey-ops-cn-check.timer"

# venv
VENV_DIR="$PREFIX/venv"
if [[ ! -d "$VENV_DIR" ]]; then
  echo "==> 创建 venv：$VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

echo "==> 安装 Python 依赖"
"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null 2>&1 || true
"$VENV_DIR/bin/pip" install -r "$PREFIX/requirements.txt"

# wrapper
echo "==> 创建命令：$BIN_LINK"
cat > "$BIN_LINK" <<EOF
#!/usr/bin/env bash
exec "$VENV_DIR/bin/python" "$PREFIX/opsctl.py" "\$@"
EOF
chmod 0755 "$BIN_LINK"

# config
echo "==> 初始化配置目录：$CONFIG_DIR"
mkdir -p "$CONFIG_DIR"
if [[ ! -f "$CONFIG_DIR/opsctl.env" ]]; then
  cp "$PREFIX/config/opsctl.env.example" "$CONFIG_DIR/opsctl.env"
  chmod 0600 "$CONFIG_DIR/opsctl.env"
  echo "  - 已生成：$CONFIG_DIR/opsctl.env（请编辑后再启用定时任务）"
else
  echo "  - 已存在：$CONFIG_DIR/opsctl.env（跳过）"
fi

if [[ ! -f "$CONFIG_DIR/origin_targets.conf" ]]; then
  cp "$PREFIX/config/origin_targets.conf.example" "$CONFIG_DIR/origin_targets.conf"
  chmod 0644 "$CONFIG_DIR/origin_targets.conf"
  echo "  - 已生成：$CONFIG_DIR/origin_targets.conf"
fi

if [[ ! -f "$CONFIG_DIR/domains.txt" ]]; then
  cp "$PREFIX/config/domains.txt.example" "$CONFIG_DIR/domains.txt"
  chmod 0644 "$CONFIG_DIR/domains.txt"
  echo "  - 已生成：$CONFIG_DIR/domains.txt"
fi

# log/state
echo "==> 创建日志/状态目录"
mkdir -p "$LOG_DIR" "$STATE_DIR"
chmod 0755 "$LOG_DIR" "$STATE_DIR"

# systemd units
if [[ "$NO_SYSTEMD" -eq 0 ]] && command -v systemctl >/dev/null 2>&1; then
  echo "==> 安装 systemd unit 文件"
  install -m 0644 "$PREFIX/systemd/onekey-ops-origin-monitor.service" /etc/systemd/system/onekey-ops-origin-monitor.service
  install -m 0644 "$PREFIX/systemd/onekey-ops-origin-monitor.timer" /etc/systemd/system/onekey-ops-origin-monitor.timer
  install -m 0644 "$PREFIX/systemd/onekey-ops-cn-check.service" /etc/systemd/system/onekey-ops-cn-check.service
  install -m 0644 "$PREFIX/systemd/onekey-ops-cn-check.timer" /etc/systemd/system/onekey-ops-cn-check.timer

  systemctl daemon-reload

  if [[ "$ENABLE_TIMERS" -eq 1 ]]; then
    echo "==> 启用并启动 timers"
    systemctl enable --now onekey-ops-origin-monitor.timer
    systemctl enable --now onekey-ops-cn-check.timer
  else
    echo "==> 未自动启用 timers（推荐你先编辑配置再启用）"
  fi
else
  echo "==> 跳过 systemd（未检测到 systemctl 或指定 --no-systemd）"
fi

echo
cat <<EOF
✅ 安装完成

下一步建议：
1) 编辑配置：
   sudo nano $CONFIG_DIR/opsctl.env
2) 编辑源站目标列表：
   sudo nano $CONFIG_DIR/origin_targets.conf
3) 编辑国内检测域名列表：
   sudo nano $CONFIG_DIR/domains.txt

手动测试运行：
   opsctl origin run
   opsctl cn run

如果你使用 systemd 并想启用定时：
   sudo systemctl enable --now onekey-ops-origin-monitor.timer
   sudo systemctl enable --now onekey-ops-cn-check.timer

查看日志：
   tail -f $LOG_DIR/origin_monitor.log
   tail -f $LOG_DIR/cn_check.log
EOF
