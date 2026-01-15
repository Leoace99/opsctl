#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""opsctl - OneKey Ops 管理工具

把你原来的两个脚本功能合并到一个可安装/可管理的 CLI：

1) origin-monitor: 直连源站探测 + 连续失败计数 + 报警
2) cn-check: 国内直连/代理探测 + 输出 JSON + 可选推送到国外服务器

建议通过 install.sh 安装到系统：
- 配置文件：/etc/onekey-ops/opsctl.env
- 列表文件：/etc/onekey-ops/origin_targets.conf, /etc/onekey-ops/domains.txt
- 日志目录：/var/log/onekey-ops
- 状态目录：/var/lib/onekey-ops

本脚本尽量不引入额外依赖（requests 仅用于 cn-check & telegram 直发）。
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import json
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

try:
    import requests  # type: ignore

    _HAS_REQUESTS = True
except Exception:
    requests = None  # type: ignore
    _HAS_REQUESTS = False


DEFAULTS: Dict[str, str] = {
    # 基础路径
    "BASE_DIR": "/opt/onekey-ops",
    "CONFIG_DIR": "/etc/onekey-ops",
    "LOG_DIR": "/var/log/onekey-ops",
    "STATE_DIR": "/var/lib/onekey-ops",

    # ---------- origin-monitor ----------
    "ORIGIN_TARGETS_FILE": "/etc/onekey-ops/origin_targets.conf",
    "ORIGIN_LOG_FILE": "/var/log/onekey-ops/origin_monitor.log",
    "ORIGIN_TIMEOUT": "5",  # curl connect/max timeout
    "ORIGIN_ALERT_INTERVAL": "3600",  # 秒
    "ORIGIN_ALERT_METHOD": "none",  # none|ssh|telegram
    "ORIGIN_SSH_KEY": "/root/.ssh/id_rsa",
    # 报警中转机（ssh 模式）。建议在 /etc/onekey-ops/opsctl.env 里配置。
    "ORIGIN_ALERT_HOST": "",
    "ORIGIN_ALERT_CMD": "/opt/telegram_send.sh",
    "ORIGIN_SSH_OPTS": "-o BatchMode=yes -o ConnectTimeout=5",
    "ORIGIN_EXPECT_HTTP_CODE": "200",
    "ORIGIN_DEFAULT_PORT": "443",
    "ORIGIN_DEFAULT_PATH": "/",
    "ORIGIN_DEFAULT_SLOW_TIME": "5",
    "ORIGIN_DEFAULT_SCHEME": "https",

    # ---------- cn-check ----------
    "CN_DOMAINS_FILE": "/etc/onekey-ops/domains.txt",
    "CN_LOG_FILE": "/var/log/onekey-ops/cn_check.log",
    "CN_RESULT_FILE": "/var/lib/onekey-ops/result_cn.json",
    "CN_TIMEOUT": "8",
    "CN_MAX_PROXY_RETRY": "2",
    "CN_PROXY_API": "",  # 【强烈建议】不要写进 git，放到 /etc/onekey-ops/opsctl.env

    # 推送到国外（可选）
    "CN_PUSH_ENABLE": "0",  # 0|1
    "CN_PUSH_USER": "root",
    # 推送目标（scp）。建议在 /etc/onekey-ops/opsctl.env 里配置。
    "CN_PUSH_HOST": "",
    "CN_PUSH_DIR": "/opt/qiangcsgfw_foreign",
    "CN_PUSH_SSH_KEY": "",  # 空=不指定 -i
    "CN_PUSH_SCP_OPTS": "-o BatchMode=yes -o ConnectTimeout=5",

    # ---------- Telegram 直发（可选） ----------
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
}


SENSITIVE_KEYWORDS = (
    "TOKEN",
    "PASSWORD",
    "PASS",
    "SECRET",
    "SIGN",
    "TRADE",
    "PROXY_API",
)


def now_str() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def append_line(path: str, line: str) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def tail_file(path: str, n: int = 200) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 4096
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                step = min(block, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
            lines = data.splitlines()[-n:]
        return "\n".join(line.decode("utf-8", errors="replace") for line in lines)
    except FileNotFoundError:
        return f"(文件不存在) {path}"


def is_sensitive_key(k: str) -> bool:
    up = k.upper()
    return any(x in up for x in SENSITIVE_KEYWORDS)


def mask_value(v: str) -> str:
    v = v.strip()
    if not v:
        return ""
    if len(v) <= 8:
        return "****"
    return v[:4] + "****" + v[-4:]


def load_env_file(path: str) -> Dict[str, str]:
    """读取 KEY=VALUE 的 env 文件（支持注释 #，支持单/双引号包裹）。"""
    cfg: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if not k:
                    continue

                # 去掉两侧引号
                if (v.startswith("\"") and v.endswith("\"")) or (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1]

                cfg[k] = v
    except FileNotFoundError:
        # 没有配置文件不报错，交给上层提示
        pass
    return cfg


def load_config(config_path: str) -> Dict[str, str]:
    cfg = dict(DEFAULTS)
    cfg.update(load_env_file(config_path))

    # 允许用环境变量临时覆盖（优先级最高）
    for k in list(cfg.keys()):
        if k in os.environ and os.environ[k].strip() != "":
            cfg[k] = os.environ[k].strip()

    return cfg


def safe_name(name: str) -> str:
    # 仅保留常见安全字符，其它替换成 _
    out = []
    for ch in name:
        if ch.isalnum() or ch in ".-_":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)


def read_int(path: str, default: int = 0) -> int:
    try:
        with open(path, "r", encoding="utf-8") as f:
            s = f.read().strip()
        return int(s) if s else default
    except Exception:
        return default


def write_int(path: str, val: int) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(val))


@dataclass
class OriginTarget:
    name: str
    domain: str
    origin_ip: str
    port: int
    path: str
    slow_time: float
    scheme: str = "https"


def parse_origin_targets(path: str, defaults: Dict[str, str]) -> List[OriginTarget]:
    targets: List[OriginTarget] = []
    default_port = int(defaults.get("ORIGIN_DEFAULT_PORT", "443") or "443")
    default_path = defaults.get("ORIGIN_DEFAULT_PATH", "/") or "/"
    default_slow = float(defaults.get("ORIGIN_DEFAULT_SLOW_TIME", "5") or "5")
    default_scheme = defaults.get("ORIGIN_DEFAULT_SCHEME", "https") or "https"

    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 3:
                    continue

                name = parts[0]
                domain = parts[1]
                origin_ip = parts[2]
                port = default_port
                pth = default_path
                slow = default_slow
                scheme = default_scheme

                if len(parts) >= 4 and parts[3]:
                    try:
                        port = int(parts[3])
                    except Exception:
                        port = default_port

                if len(parts) >= 5 and parts[4]:
                    pth = parts[4]

                if not pth.startswith("/"):
                    pth = "/" + pth

                if len(parts) >= 6 and parts[5]:
                    try:
                        slow = float(parts[5])
                    except Exception:
                        slow = default_slow

                if len(parts) >= 7 and parts[6]:
                    scheme = parts[6].lower()

                targets.append(
                    OriginTarget(
                        name=name,
                        domain=domain,
                        origin_ip=origin_ip,
                        port=port,
                        path=pth,
                        slow_time=slow,
                        scheme=scheme,
                    )
                )
    except FileNotFoundError:
        return []

    return targets


def run_curl_probe(target: OriginTarget, timeout: int) -> Tuple[str, float, str]:
    """返回 (http_code, time_total, curl_error)"""
    if shutil.which("curl") is None:
        return "000", 0.0, "curl_not_found"

    url = f"{target.scheme}://{target.domain}{target.path}"
    resolve = f"{target.domain}:{target.port}:{target.origin_ip}"

    cmd = [
        "curl",
        "-k",
        "-s",
        "-o",
        "/dev/null",
        "--connect-timeout",
        str(timeout),
        "--max-time",
        str(timeout),
        "-w",
        "%{http_code} %{time_total}",
        "--resolve",
        resolve,
        url,
    ]

    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2)
        out = (p.stdout or "").strip()
        if p.returncode != 0 and not out:
            return "000", 0.0, f"curl_rc={p.returncode}"

        # out 形如："200 0.123"
        pieces = out.split()
        if len(pieces) >= 2:
            code = pieces[0]
            try:
                t = float(pieces[1])
            except Exception:
                t = 0.0
            return code, t, ""

        return "000", 0.0, "curl_parse_error"
    except subprocess.TimeoutExpired:
        return "000", float(timeout), "curl_timeout"
    except Exception as e:
        return "000", 0.0, f"curl_exc={type(e).__name__}"


def send_alert_origin(cfg: Dict[str, str], msg: str) -> Tuple[bool, str]:
    method = (cfg.get("ORIGIN_ALERT_METHOD") or "none").strip().lower()
    if method in ("", "none", "off", "false", "0"):
        return True, "alert_disabled"

    if method == "ssh":
        host = (cfg.get("ORIGIN_ALERT_HOST") or "").strip()
        cmd = (cfg.get("ORIGIN_ALERT_CMD") or "").strip()
        key = (cfg.get("ORIGIN_SSH_KEY") or "").strip()
        ssh_opts = (cfg.get("ORIGIN_SSH_OPTS") or "").strip()

        if not host or not cmd:
            return False, "missing ORIGIN_ALERT_HOST/ORIGIN_ALERT_CMD"

        argv = ["ssh"]
        if ssh_opts:
            argv += shlex.split(ssh_opts)
        if key:
            argv += ["-i", key]

        # 关键：把消息做 shell quote，确保中文/空格/emoji 不会被拆参数
        remote = f"{cmd} {shlex.quote(msg)}"
        argv += [host, remote]

        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=15)
            if p.returncode == 0:
                return True, "ssh_ok"
            return False, f"ssh_failed rc={p.returncode} stderr={p.stderr.strip()[:200]}"
        except Exception as e:
            return False, f"ssh_exc={type(e).__name__}"

    if method in ("telegram", "tg"):
        if not _HAS_REQUESTS:
            return False, "python requests not installed"

        token = (cfg.get("TELEGRAM_BOT_TOKEN") or "").strip()
        chat_id = (cfg.get("TELEGRAM_CHAT_ID") or "").strip()
        if not token or not chat_id:
            return False, "missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID"

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            r = requests.post(url, data={"chat_id": chat_id, "text": msg}, timeout=10)
            if r.status_code == 200:
                return True, "telegram_ok"
            return False, f"telegram_http={r.status_code}"
        except Exception as e:
            return False, f"telegram_exc={type(e).__name__}"

    return False, f"unknown alert method: {method}"


def cmd_origin_run(cfg: Dict[str, str]) -> int:
    targets_file = cfg.get("ORIGIN_TARGETS_FILE", "")
    log_file = cfg.get("ORIGIN_LOG_FILE", "")
    state_dir = os.path.join(cfg.get("STATE_DIR", "/var/lib/onekey-ops"), "origin")

    timeout = int(cfg.get("ORIGIN_TIMEOUT", "5") or "5")
    alert_interval = int(cfg.get("ORIGIN_ALERT_INTERVAL", "3600") or "3600")
    expect_code = (cfg.get("ORIGIN_EXPECT_HTTP_CODE", "200") or "200").strip()

    if not targets_file or not os.path.exists(targets_file):
        eprint(f"targets 文件不存在：{targets_file}")
        return 2

    targets = parse_origin_targets(targets_file, cfg)
    if not targets:
        eprint(f"targets 文件为空或解析失败：{targets_file}")
        return 2

    ensure_dir(state_dir)

    for t in targets:
        safe = safe_name(t.name)
        count_file = os.path.join(state_dir, f"{safe}.fail")
        last_alert_file = os.path.join(state_dir, f"{safe}.last_alert")

        code, total, curl_err = run_curl_probe(t, timeout=timeout)

        fail_reason = ""
        if code != expect_code:
            # curl 失败时 code 可能为 000
            fail_reason = f"HTTP {code}" if code != "000" else (curl_err or "connect_fail")
        elif total > t.slow_time:
            fail_reason = f"响应慢 {total:.3f}s"

        if not fail_reason:
            # 恢复
            if os.path.exists(count_file) or os.path.exists(last_alert_file):
                try:
                    os.remove(count_file)
                except FileNotFoundError:
                    pass
                try:
                    os.remove(last_alert_file)
                except FileNotFoundError:
                    pass

            append_line(log_file, f"{now_str()} | {t.name} | OK | code={code} time={total:.3f}s")
            continue

        # 连续失败计数
        count = read_int(count_file, default=0) + 1
        write_int(count_file, count)

        # 报警策略：前 10 次每次报警，之后每小时一次
        now_ts = int(_dt.datetime.now().timestamp())
        last_ts = read_int(last_alert_file, default=0)

        send = False
        if count <= 10:
            send = True
        elif count > 10 and (now_ts - last_ts) >= alert_interval:
            send = True

        if send:
            msg = (
                f"🚨【直连源站疑似被打】 | 名称: {t.name} | 域名: {t.domain} | IP: {t.origin_ip} | "
                f"原因: {fail_reason} | 响应时间: {total:.3f}s | 连续异常: {count}次 | 时间: {now_str()}"
            )
            ok, detail = send_alert_origin(cfg, msg)
            append_line(log_file, f"{now_str()} | {t.name} | ALERT send={ok} detail={detail}")
            if ok:
                write_int(last_alert_file, now_ts)

        append_line(
            log_file,
            f"{now_str()} | {t.name} | FAIL_REASON={fail_reason} | code={code} time={total:.3f}s | COUNT={count}",
        )

    return 0


# ---------------- cn-check ----------------

def classify_error(e: Exception) -> str:
    s = str(e).lower()
    if "timed out" in s or "timeout" in s:
        return "timeout"
    if "reset" in s:
        return "reset"
    if "ssl" in s or "eof" in s:
        return "ssl_error"
    if "refused" in s:
        return "refused"
    if "proxy" in s and "connect" in s:
        return "proxy_connect"
    return "other"


def get_proxy(cfg: Dict[str, str]) -> Optional[Dict[str, str]]:
    if not _HAS_REQUESTS:
        return None

    api = (cfg.get("CN_PROXY_API") or "").strip()
    if not api:
        return None

    try:
        r = requests.get(api, timeout=5)
        t = (r.text or "").strip().splitlines()[0].strip()
        if not t:
            return None

        # 兼容：
        # 1) ip:port:user:pwd
        # 2) ip:port
        # 3) user:pwd@ip:port
        # 4) http://user:pwd@ip:port
        if t.startswith("http://") or t.startswith("https://"):
            proxy_url = t
        elif "@" in t:
            proxy_url = "http://" + t
        else:
            parts = t.split(":")
            if len(parts) == 4:
                ip, port, user, pwd = parts
                proxy_url = f"http://{user}:{pwd}@{ip}:{port}"
            elif len(parts) == 2:
                ip, port = parts
                proxy_url = f"http://{ip}:{port}"
            else:
                return None

        return {"http": proxy_url, "https": proxy_url}
    except Exception:
        return None


def request_check(
    url: str,
    timeout: int,
    verify: bool,
    proxies: Optional[Dict[str, str]] = None,
    http_timeout_is_unstable: bool = False,
) -> str:
    if not _HAS_REQUESTS:
        return "❌ requests 未安装"

    try:
        r = requests.get(
            url,
            timeout=timeout,
            verify=verify,
            proxies=proxies,
            allow_redirects=True,
            headers={"User-Agent": "opsctl/1.0"},
        )
        code = r.status_code
        if 200 <= code < 400:
            return f"✅ 正常({code})"
        return f"⚠️ 异常({code})"
    except Exception as e:
        err = classify_error(e)
        if http_timeout_is_unstable and err == "timeout":
            return "⚠️ 不稳定(timeout)"
        return f"❌ 不可用({err})"


def direct_check_domain(domain: str, timeout: int) -> Tuple[str, str]:
    https_status = request_check(f"https://{domain}", timeout=timeout, verify=False)
    http_status = request_check(
        f"http://{domain}",
        timeout=timeout,
        verify=True,
        http_timeout_is_unstable=True,
    )
    return https_status, http_status


def proxy_check_domain(domain: str, timeout: int, max_retry: int, cfg: Dict[str, str]) -> Tuple[str, str]:
    proxy_https = "❌ 不可用"
    proxy_http = "❌ 不可用"

    if not (cfg.get("CN_PROXY_API") or "").strip():
        return "⚠️ 未配置代理API", "⚠️ 未配置代理API"

    for _ in range(max_retry):
        proxy = get_proxy(cfg)
        if not proxy:
            continue

        proxy_https = request_check(
            f"https://{domain}",
            timeout=timeout,
            verify=False,
            proxies=proxy,
        )
        proxy_http = request_check(
            f"http://{domain}",
            timeout=timeout,
            verify=True,
            proxies=proxy,
        )

        if proxy_https.startswith("✅") or proxy_http.startswith("✅"):
            break

    return proxy_https, proxy_http


def read_domains(path: str) -> List[str]:
    domains: List[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                domains.append(line)
    except FileNotFoundError:
        return []

    return domains


def scp_push(cfg: Dict[str, str], local_file: str, log_file: str) -> bool:
    if shutil.which("scp") is None:
        append_line(log_file, f"{now_str()} | push | scp_not_found")
        return False

    user = (cfg.get("CN_PUSH_USER") or "").strip()
    host = (cfg.get("CN_PUSH_HOST") or "").strip()
    rdir = (cfg.get("CN_PUSH_DIR") or "").strip()

    if not user or not host or not rdir:
        append_line(log_file, f"{now_str()} | push | missing CN_PUSH_USER/CN_PUSH_HOST/CN_PUSH_DIR")
        return False

    scp_opts = (cfg.get("CN_PUSH_SCP_OPTS") or "").strip()
    key = (cfg.get("CN_PUSH_SSH_KEY") or "").strip()

    argv = ["scp"]
    if scp_opts:
        argv += shlex.split(scp_opts)
    if key:
        argv += ["-i", key]

    argv += [local_file, f"{user}@{host}:{rdir.rstrip('/')}/"]

    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        if p.returncode == 0:
            append_line(log_file, f"{now_str()} | push | ok | {local_file} -> {user}@{host}:{rdir}")
            return True
        append_line(log_file, f"{now_str()} | push | fail rc={p.returncode} stderr={p.stderr.strip()[:200]}")
        return False
    except Exception as e:
        append_line(log_file, f"{now_str()} | push | exc={type(e).__name__}")
        return False


def cmd_cn_run(cfg: Dict[str, str], push_override: Optional[bool] = None) -> int:
    if not _HAS_REQUESTS:
        eprint("未安装 requests：请先执行 install.sh（会自动装依赖）")
        return 3

    domains_file = cfg.get("CN_DOMAINS_FILE", "")
    log_file = cfg.get("CN_LOG_FILE", "")
    json_file = cfg.get("CN_RESULT_FILE", "")

    timeout = int(cfg.get("CN_TIMEOUT", "8") or "8")
    max_retry = int(cfg.get("CN_MAX_PROXY_RETRY", "2") or "2")

    if not domains_file or not os.path.exists(domains_file):
        eprint(f"域名列表文件不存在：{domains_file}")
        return 2

    domains = read_domains(domains_file)
    if not domains:
        eprint(f"域名列表为空：{domains_file}")
        return 2

    append_line(log_file, f"[{now_str()}] 开始国内检测任务 domains={len(domains)}")

    results: List[Dict[str, str]] = []
    now = now_str()

    proxy_enabled = bool((cfg.get("CN_PROXY_API") or "").strip())

    for domain in domains:
        d_https, d_http = direct_check_domain(domain, timeout)
        p_https, p_http = proxy_check_domain(domain, timeout, max_retry, cfg)

        # 最终判断（沿用你原来的规则 + 更健壮的 ok 判断）
        d_https_ok = d_https.startswith("✅")
        p_https_ok = isinstance(p_https, str) and p_https.startswith("✅")
        p_http_ok = isinstance(p_http, str) and p_http.startswith("✅")

        if not proxy_enabled:
            # 没有代理 API 时，只能做“国内直连”判断
            final = "✅ 国内访问正常（未启用代理验证）" if d_https_ok else "🚫 国内异常（未启用代理验证）"
        else:
            if d_https_ok:
                if isinstance(p_https, str) and p_https.startswith("❌"):
                    final = "⚠️ 国内可访问，但存在出口差异"
                else:
                    final = "✅ 国内访问正常"
            else:
                if p_https_ok or p_http_ok:
                    final = "⚠️ 国内异常（HTTPS 受阻）"
                else:
                    final = "🚫 国内无法访问（需海外验证）"

        log_block = (
            f"{now} | {domain}\n"
            f"  直连HTTPS状态: {d_https}\n"
            f"  直连HTTP状态: {d_http}\n"
            f"  代理HTTPS状态: {p_https}\n"
            f"  代理HTTP状态: {p_http}\n"
            f"  最终判断: {final}\n"
        )

        print(log_block)
        append_line(log_file, log_block.rstrip("\n"))

        results.append(
            {
                "domain": domain,
                "direct_https": d_https,
                "direct_http": d_http,
                "proxy_https": p_https,
                "proxy_http": p_http,
                "final_cn": final,
            }
        )

    ensure_dir(os.path.dirname(json_file))
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    append_line(log_file, f"[{now_str()}] ✅ result 已写入 {json_file}")

    # 推送（可选）
    push_enabled = (cfg.get("CN_PUSH_ENABLE", "0") or "0").strip() in ("1", "true", "yes", "on")
    if push_override is not None:
        push_enabled = push_override

    if push_enabled:
        ok = scp_push(cfg, json_file, log_file)
        if ok:
            append_line(log_file, f"[{now_str()}] ✅ result 已推送")
        else:
            append_line(log_file, f"[{now_str()}] ❌ 推送失败")

    append_line(log_file, f"[{now_str()}] 国内任务完成")
    append_line(log_file, "------------------------------------")
    return 0


def cmd_cn_push(cfg: Dict[str, str], file_path: Optional[str]) -> int:
    log_file = cfg.get("CN_LOG_FILE", "")
    json_file = file_path or cfg.get("CN_RESULT_FILE", "")

    if not json_file or not os.path.exists(json_file):
        eprint(f"要推送的文件不存在：{json_file}")
        return 2

    ok = scp_push(cfg, json_file, log_file)
    return 0 if ok else 1


# ---------------- config / status / systemd ----------------


def cmd_config_show(cfg: Dict[str, str], config_path: str) -> int:
    print(f"Config file: {config_path}")
    for k in sorted(cfg.keys()):
        v = cfg[k]
        if is_sensitive_key(k):
            v = mask_value(v)
        print(f"{k}={v}")
    return 0


def cmd_logs(cfg: Dict[str, str], which: str, lines: int) -> int:
    if which == "origin":
        p = cfg.get("ORIGIN_LOG_FILE", "")
    elif which == "cn":
        p = cfg.get("CN_LOG_FILE", "")
    else:
        eprint("logs 只支持 origin 或 cn")
        return 2

    print(tail_file(p, n=lines))
    return 0


def systemctl(*args: str) -> Tuple[int, str, str]:
    if shutil.which("systemctl") is None:
        return 127, "", "systemctl_not_found"
    p = subprocess.run(["systemctl", *args], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def cmd_systemd_status() -> int:
    units = [
        "onekey-ops-origin-monitor.timer",
        "onekey-ops-cn-check.timer",
        "onekey-ops-origin-monitor.service",
        "onekey-ops-cn-check.service",
    ]
    for u in units:
        rc, out, err = systemctl("status", u, "--no-pager")
        print("=" * 80)
        print(f"$ systemctl status {u}  (rc={rc})")
        if out.strip():
            print(out.strip())
        if err.strip():
            print(err.strip())
    return 0


def cmd_status(cfg: Dict[str, str], config_path: str) -> int:
    print("OneKey Ops 状态摘要")
    print("=" * 60)

    def fmt_exist(p: str) -> str:
        if not p:
            return "(未设置)"
        return "✅" if os.path.exists(p) else "❌"

    print(f"config: {config_path} {fmt_exist(config_path)}")
    print(f"origin targets: {cfg.get('ORIGIN_TARGETS_FILE')} {fmt_exist(cfg.get('ORIGIN_TARGETS_FILE',''))}")
    print(f"domains: {cfg.get('CN_DOMAINS_FILE')} {fmt_exist(cfg.get('CN_DOMAINS_FILE',''))}")
    print(f"origin log: {cfg.get('ORIGIN_LOG_FILE')} {fmt_exist(cfg.get('ORIGIN_LOG_FILE',''))}")
    print(f"cn log: {cfg.get('CN_LOG_FILE')} {fmt_exist(cfg.get('CN_LOG_FILE',''))}")
    print(f"cn result: {cfg.get('CN_RESULT_FILE')} {fmt_exist(cfg.get('CN_RESULT_FILE',''))}")

    # systemd 简要
    if shutil.which("systemctl") is not None:
        for timer in ["onekey-ops-origin-monitor.timer", "onekey-ops-cn-check.timer"]:
            rc, out, err = systemctl("is-enabled", timer)
            enabled = out.strip() if out.strip() else err.strip()
            print(f"systemd {timer}: {enabled} (rc={rc})")
    else:
        print("systemd: systemctl 不存在（可能不是 systemd 系统）")

    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="opsctl",
        formatter_class=argparse.RawTextHelpFormatter,
        description="OneKey Ops 管理工具（origin-monitor + cn-check）",
    )
    p.add_argument(
        "--config",
        default=os.environ.get("OPSCTL_CONFIG", DEFAULTS["CONFIG_DIR"] + "/opsctl.env"),
        help="配置文件路径（默认 /etc/onekey-ops/opsctl.env，可用 OPSCTL_CONFIG 覆盖）",
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("origin", help="直连源站监控")
    sub_origin = sp.add_subparsers(dest="sub", required=True)
    sub_origin.add_parser("run", help="执行一次探测")

    sp = sub.add_parser("cn", help="国内/代理检测")
    sub_cn = sp.add_subparsers(dest="sub", required=True)
    sub_cn_run = sub_cn.add_parser("run", help="执行一次检测并写入 JSON")
    sub_cn_run.add_argument("--push", action="store_true", help="强制推送（忽略 CN_PUSH_ENABLE）")
    sub_cn_run.add_argument("--no-push", action="store_true", help="强制不推送（忽略 CN_PUSH_ENABLE）")

    sub_cn_push = sub_cn.add_parser("push", help="仅推送已有 JSON")
    sub_cn_push.add_argument("--file", default=None, help="要推送的 JSON 文件（默认 CN_RESULT_FILE）")

    sp = sub.add_parser("config", help="查看配置")
    sub_cfg = sp.add_subparsers(dest="sub", required=True)
    sub_cfg.add_parser("show", help="打印配置（敏感字段会打码）")

    sp = sub.add_parser("logs", help="查看日志 tail")
    sp.add_argument("which", choices=["origin", "cn"], help="查看哪个日志")
    sp.add_argument("--lines", type=int, default=200, help="显示行数")

    sub.add_parser("status", help="查看整体状态")

    sp = sub.add_parser("systemd", help="systemd 状态查看")
    sub_sys = sp.add_subparsers(dest="sub", required=True)
    sub_sys.add_parser("status", help="systemctl status (timers/services)")

    return p


def main(argv: List[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    cfg = load_config(args.config)

    if args.cmd == "origin" and args.sub == "run":
        return cmd_origin_run(cfg)

    if args.cmd == "cn" and args.sub == "run":
        push_override: Optional[bool] = None
        if getattr(args, "push", False):
            push_override = True
        if getattr(args, "no_push", False):
            push_override = False
        return cmd_cn_run(cfg, push_override=push_override)

    if args.cmd == "cn" and args.sub == "push":
        return cmd_cn_push(cfg, file_path=getattr(args, "file", None))

    if args.cmd == "config" and args.sub == "show":
        return cmd_config_show(cfg, args.config)

    if args.cmd == "logs":
        return cmd_logs(cfg, which=args.which, lines=args.lines)

    if args.cmd == "status":
        return cmd_status(cfg, args.config)

    if args.cmd == "systemd" and args.sub == "status":
        return cmd_systemd_status()

    eprint("未知命令")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
