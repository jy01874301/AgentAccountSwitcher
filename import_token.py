#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
import_token.py —— TraeWork CN 凭据导入工具（独立脚本，零依赖、零联网）

把「别机导出的 tokens JSON」（export_tokens.py -o 的产物）或「原版 storage.json」
里的 refresh_token / 设备密钥对 (icube-dc) 等凭据，合并进 config.json 的 tokens
数组，供 trae_work_checkin.py 在无需本机扫码登录的前提下远程续期 / 签到使用。

可接受的输入（均可多个，自动去重）：
  1) export_tokens.py 的产物文件
       例如 D:\\别机\\config.json          （含 "tokens":[...]）
  2) 手动写的凭据 JSON
       {"label":..,"token":..,"refresh_token":..,"private_key_pem":"-----...","..."}
  3) 原始 storage.json（自解密登录态，抽取 refresh_token 与设备密钥对）

用法：
  python import_token.py 输入文件... -o "D:\\AI项目\\自动签到\\config.json"
  python import_token.py 输入文件... --print
  python import_token.py 输入文件... --list-keys --output ...

  -o FILE       输出文件（默认 <当前目录>/config.json；已存在则只覆写 tokens 字段）
  --print       只打印将写入的 tokens JSON，不写文件
  --list-keys   只打印解析结果明细，不写文件、不打印完整凭据

说明（凭据语义）：
  - 续期走 ExchangeToken 时需要 device_id + private_key_pem(icube-dc) 构造
    DeviceProof。refresh_token 是账号级可再生凭据；private_key_pem 绑定设备。
  - 从别机整份 storage.json 导入时，private_key_pem 是其设备私钥，属「模拟该设备」
    远程续期；跨设备（本机新私钥 + 别机 refresh_token）能否通过由服务端校验，
    请在导入后运行 trae_work_checkin.py --refresh 实测。
安全提醒：本脚本会把 refresh_token / 私钥写入输出文件，等同长期登录凭据，
         请勿提交仓库或外传；POSIX 上会自动收紧权限 0600。
"""

import argparse
import base64
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

AUTH_KEY = "iCubeAuthInfo://icube.cloudide"
DEVICE_PREFIX = "iCubeAuthInfo://icube-dc:"
OUTPUT_KEYS = ("label", "token", "device_id", "region", "refresh_token",
               "machine_id", "private_key_pem", "public_key_pem")
DEFAULT_OUTPUT = "config.json"

_URE = bytes([
    82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251,
    124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203,
    84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78,
    8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37])
_DRE = bytes([
    31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95,
    96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239,
    160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97,
    23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125])

_SBOX = [0] * 256
_INV_SBOX = [0] * 256
_READY = False


def _xtime(a):
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _build_tables():
    global _READY
    if _READY:
        return
    exp = [0] * 255
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x ^= _xtime(x)
    for a in range(256):
        inv = 0 if a == 0 else exp[(255 - log[a]) % 255]
        r = 0
        for i in range(8):
            bit = ((inv >> i) ^ (inv >> ((i + 4) % 8)) ^ (inv >> ((i + 5) % 8))
                   ^ (inv >> ((i + 6) % 8)) ^ (inv >> ((i + 7) % 8)) ^ (0x63 >> i)) & 1
            r |= bit << i
        _SBOX[a] = r
    for a, v in enumerate(_SBOX):
        _INV_SBOX[v] = a
    _READY = True


def _gmul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _key_expansion(key):
    _build_tables()
    w = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= _RCON[i // 4 - 1]
        w.append([p ^ q for p, q in zip(w[i - 4], t)])
    return w


def _decrypt_block(key16, ct16):
    w = _key_expansion(key16)
    st = list(ct16)
    for i in range(16):
        st[i] ^= w[40 + i // 4][i % 4]
    for rnd in range(9, 0, -1):
        out = st[:]
        for r in range(1, 4):
            for c in range(4):
                out[r + 4 * c] = st[r + 4 * ((c - r) % 4)]
        st = [_INV_SBOX[b] for b in out]
        for i in range(16):
            st[i] ^= w[rnd * 4 + i // 4][i % 4]
        for c in range(4):
            s = st[4 * c:4 * c + 4]
            st[4 * c + 0] = _gmul(0x0E, s[0]) ^ _gmul(0x0B, s[1]) ^ _gmul(0x0D, s[2]) ^ _gmul(0x09, s[3])
            st[4 * c + 1] = _gmul(0x09, s[0]) ^ _gmul(0x0E, s[1]) ^ _gmul(0x0B, s[2]) ^ _gmul(0x0D, s[3])
            st[4 * c + 2] = _gmul(0x0D, s[0]) ^ _gmul(0x09, s[1]) ^ _gmul(0x0E, s[2]) ^ _gmul(0x0B, s[3])
            st[4 * c + 3] = _gmul(0x0B, s[0]) ^ _gmul(0x0D, s[1]) ^ _gmul(0x09, s[2]) ^ _gmul(0x0E, s[3])
    out = st[:]
    for r in range(1, 4):
        for c in range(4):
            out[r + 4 * c] = st[r + 4 * ((c - r) % 4)]
    st = [_INV_SBOX[b] for b in out]
    for i in range(16):
        st[i] ^= w[i // 4][i % 4]
    return bytes(st)


def _cbc_decrypt(key16, iv, data):
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        dec = _decrypt_block(key16, block)
        out.extend(d ^ p for d, p in zip(dec, prev))
        prev = block
    pad = out[-1]
    if not 1 <= pad <= 16 or out[-pad:] != bytes([pad]) * pad:
        raise ValueError("PKCS#7 填充校验失败")
    return bytes(out[:-pad])


def decrypt_auth(b64_text):
    text = b64_text.strip().replace("-", "+").replace("_", "/")
    text += "=" * (-len(text) % 4)
    t = base64.b64decode(text)
    if len(t) < 54:
        raise ValueError("登录态密文过短")
    sha = hashlib.sha512(t[6:38]).digest()
    xored = bytes(a ^ b for a, b in zip(_URE, _DRE))
    h = hashlib.sha512(sha + xored).digest()
    plain = _cbc_decrypt(h[:16], h[16:32], t[38:])
    return json.loads(plain[64:].decode("utf-8"))


_ALIAS = {
    "token": "accessToken",
    "refresh_token": "refreshToken",
    "private_key_pem": "privateKeyPEM",
    "public_key_pem": "publicKeyPEM",
    "machine_id": "machineId",
}


def _normalize(entry):
    if not isinstance(entry, dict):
        return None
    out = {}
    for k in OUTPUT_KEYS:
        v = entry.get(k)
        alias = _ALIAS.get(k)
        if alias:
            v = v or entry.get(alias)
        out[k] = str(v or "")
    return out


def from_tokens_payload(data):
    if isinstance(data, dict):
        if isinstance(data.get("tokens"), list):
            return list(data["tokens"])
        if data.get("token"):
            return [data]
        return []
    if isinstance(data, list):
        return data
    return []


def from_storage(data):
    enc = data.get(AUTH_KEY)
    if not enc:
        return None
    try:
        auth = enc if isinstance(enc, dict) else decrypt_auth(enc)
    except Exception as e:
        raise ValueError("登录态解密失败: %s" % e)
    token = auth.get("token") or auth.get("accessToken")
    if not token:
        raise ValueError("登录态中没有 token")
    region = ""
    ur = auth.get("userRegion")
    if isinstance(ur, dict):
        region = ur.get("region") or ""
    device_id = ""
    for k in data:
        if isinstance(k, str) and k.startswith(DEVICE_PREFIX):
            did = k.split(":", 2)[-1]
            if did and did != "0":
                device_id = did
                break
    private_key_pem, public_key_pem = "", ""
    dc_key = next((k for k in data
                   if isinstance(k, str) and k.startswith(DEVICE_PREFIX)), None)
    if dc_key:
        try:
            dc = data[dc_key]
            dc = dc if isinstance(dc, dict) else decrypt_auth(dc)
            private_key_pem = str(dc.get("privateKeyPEM") or "")
            public_key_pem = str(dc.get("publicKeyPEM") or "")
        except Exception:
            pass
    label = "imported"
    acct = auth.get("account")
    email = str((acct or {}).get("email") or (acct or {}).get("username") or "")
    if email:
        label = email
    return _normalize({
        "label": label, "token": token, "device_id": device_id, "region": region,
        "refresh_token": str(auth.get("refreshToken") or ""),
        "machine_id": str(data.get("telemetry.machineId") or ""),
        "private_key_pem": private_key_pem, "public_key_pem": public_key_pem,
    })


def load_input(path, say):
    p = Path(path)
    if not p.is_file():
        say("[warn] 输入文件不存在: %s" % p)
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        say("[跳过] %s: 不是有效 JSON（%r）" % (p, e))
        return []
    if isinstance(data, dict) and data.get(AUTH_KEY) and not data.get("tokens"):
        try:
            e = from_storage(data)
        except Exception as ex:
            say("[跳过] %s: %s" % (p, ex))
            return []
        if not e:
            say("[跳过] %s: storage.json 未登录" % p)
            return []
        say("[解析] %s  -> 整份 storage.json  label=%s device_id=%s"
            % (p.name, e["label"], e["device_id"] or "-"))
        return [e]
    raw = from_tokens_payload(data)
    if not raw:
        say("[跳过] %s: 既非登录态 storage.json，也非 tokens 载荷" % p)
        return []
    out = []
    for entry in raw:
        e = _normalize(entry)
        if not e or not e["token"]:
            say("[跳过] %s: 条目缺少 token" % p)
            continue
        out.append(e)
    say("[解析] %s  -> tokens 载荷，%d 个条目" % (p.name, len(out)))
    return out


def _atomic_write_text(path, text):
    """原子落盘：先写**同目录**临时文件，fsync 后再 os.replace 覆盖目标。

    ⚠️ 原先直接 `out.write_text(...)` 覆写含 refresh_token 的 config.json ——
    中途被打断（Ctrl+C / 断电 / 杀软锁文件）会留下**截断的**文件，
    等于**全部账号凭据一次性丢失**。`os.replace` 在同一卷上是原子的：
    读到的要么是旧内容、要么是新内容，没有中间态（见 AUDIT_2026-09-24.md P2-12）。

    临时文件必须与目标同目录 —— 跨卷时 os.replace 会退化成「复制 + 删除」，就不原子了。
    """
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                    dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:  # noqa: BLE001  含 KeyboardInterrupt —— 临时文件别留下
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def merge_write(output, tokens):
    out = Path(output).resolve()
    if out.is_dir():
        raise SystemExit("输出路径 %s 是目录" % out)
    if out.is_file():
        try:
            data = json.loads(out.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("根节点不是 JSON 对象")
        except (OSError, ValueError) as e:
            raise SystemExit("目标文件 %s 不是有效的 JSON 配置（%r），未写入" % (out, e))
        data["tokens"] = tokens
        _atomic_write_text(out, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    else:
        _atomic_write_text(out, json.dumps({"tokens": tokens}, ensure_ascii=False, indent=2) + "\n")
    if os.name == "posix":
        try:
            os.chmod(out, 0o600)
        except OSError:
            pass
    return out


def main():
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(
        description="导入 TraeWork CN 凭据（refresh_token/设备密钥对）到 config.json 的 tokens 数组")
    ap.add_argument("inputs", nargs="*", help="输入文件：tokens JSON 或 storage.json")
    ap.add_argument("-o", "--output", default=DEFAULT_OUTPUT,
                    help="输出文件（默认 %(default)s，相对当前目录）")
    ap.add_argument("--print", dest="do_print", action="store_true",
                    help="只打印将写入的 tokens JSON，不写文件")
    ap.add_argument("--list-keys", action="store_true",
                    help="只打印解析结果，不写文件、不打印完整凭据")
    args = ap.parse_args()

    def say(msg):
        (sys.stderr if args.do_print or args.list_keys else sys.stdout).write(msg + "\n")

    if not args.inputs:
        raise SystemExit("未提供输入文件：给出 export_tokens 的 JSON 或 storage.json（可多个）")

    tokens, seen = [], set()
    for path in args.inputs:
        for e in load_input(path, say):
            t = e.get("token") or ""
            if t and t in seen:
                say("[跳过] %s: 与已解析账号 token 重复" % e["label"])
                continue
            seen.add(t)
            tokens.append(e)

    if not tokens:
        raise SystemExit("没有可导入的账号")

    if args.list_keys:
        for e in tokens:
            say("[条目] label=%s  device_id=%s  region=%s  有refresh=%s  有私钥=%s"
                % (e["label"], e["device_id"] or "-", e["region"] or "-",
                   "Y" if e["refresh_token"] else "N",
                   "Y" if e["private_key_pem"] else "N"))
        return

    if args.do_print:
        print(json.dumps({"tokens": tokens}, ensure_ascii=False, indent=2))
        return

    out = merge_write(args.output, tokens)
    say("已把 %d 个账号写入 %s 的 tokens 字段（已有其他配置保留，旧 tokens 被本批覆盖）" % (len(tokens), out))
    say("实测远程续期：请在自动签到目录运行  python trae_work_checkin.py --refresh")


if __name__ == "__main__":
    main()