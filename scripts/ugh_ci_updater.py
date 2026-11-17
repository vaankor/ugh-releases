#!/usr/bin/env python3
"""Файлы обновления Ultimate Gaming Hub (Tauri Updater): описание изменений, манифест, проверка подписи.

Ничего не публикует и не читает закрытый ключ: подписывает файл `tauri signer sign` в workflow,
здесь — только независимая проверка того, что получилось (тем же алгоритмом minisign, что в плагине).

Команды (запуск из корня репозитория):
  notes    --version X.Y.Z [--out FILE]            раздел версии из CHANGELOG.md (+ CHANGELOG.en.md / .uk.md)
  pubkey   [--config FILE] [--repo OWNER/NAME]     открытый ключ задан и адрес обновлений указывает на репозиторий релизов
  manifest --version --file --sig --url --notes --out [--pub-date]   собрать latest.json
  verify   --manifest --file --version [--pubkey-file | --config] [--repo] [--url]   проверить манифест и подпись
  embedded --nsis FILE (--pubkey-file | --config) [--endpoint URL]   в exe программы внутри NSIS-пакета
           встроены именно этот открытый ключ (и адрес обновлений) — защита от сборки с подменённым ключом
  compare-release --release DIR --candidate DIR --version --pubkey-file --repo --assets FILE [--body FILE]
           перед переводом пре-релиза в стабильный: ВСЕ файлы пре-релиза совпадают с кандидатом сборки

--allow-local-http (manifest, verify) — только для сквозной проверки на машине CI: разрешает адреса
http://127.0.0.1 / http://localhost. В релизах не используется.

Формат CHANGELOG.md:
  ## [1.22.0] — 2026-11-20
  ### Новое
  - …
Заголовок версии обязан содержать дату ГГГГ-ММ-ДД; в разделе должен быть хотя бы один пункт «- ».
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
PLACEHOLDER = "UGH_UPDATER_PUBKEY_NOT_SET"
PLATFORM = "windows-x86_64"
LANG_FILES = {"en": "CHANGELOG.en.md", "uk": "CHANGELOG.uk.md"}
# Заголовок версии: «## [1.22.0] — 2026-11-20», «## 1.22.0 - 2026-11-20»
HEADING = re.compile(r"^##\s+\[?v?(\d+\.\d+\.\d+)\]?\s*(?:[—–-]\s*(.*?))?\s*$")
FORBIDDEN = re.compile(r"\b(TODO|TBD|FIXME|XXX)\b|не выпущен|unreleased", re.I)
MAX_NOTES = 20000


class Fail(Exception):
    pass


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------- Описание изменений ----------

def changelog_section(text, version):
    """Текст раздела версии и дата из заголовка."""
    lines = text.splitlines()
    start = None
    date = None
    for i, line in enumerate(lines):
        m = HEADING.match(line)
        if m and m.group(1) == version:
            if start is not None:
                raise Fail(f"версия {version} указана в журнале изменений дважды")
            start, date = i, (m.group(2) or "").strip()
    if start is None:
        raise Fail(f"в журнале изменений нет раздела для версии {version}")
    body = []
    for line in lines[start + 1:]:
        if HEADING.match(line) or re.match(r"^#\s", line):
            break
        body.append(line)
    return "\n".join(body).strip(), date


def check_section(body, date, version, where):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        raise Fail(f"{where}: в заголовке версии {version} нет даты ГГГГ-ММ-ДД (сейчас: {date!r})")
    try:
        dt.date.fromisoformat(date)
    except ValueError:
        raise Fail(f"{where}: неверная дата {date!r}")
    if not re.search(r"^\s*[-*]\s+\S", body, re.M):
        raise Fail(f"{where}: в разделе версии {version} нет ни одного пункта «- …»")
    if FORBIDDEN.search(body) or FORBIDDEN.search(date):
        raise Fail(f"{where}: в разделе версии {version} осталась заготовка (TODO/«не выпущена»)")
    if "<!--" in body:
        raise Fail(f"{where}: HTML-комментарии в описании изменений не поддерживаются (ими отмечаются языки)")


def build_notes(version, changelog="CHANGELOG.md"):
    """Описание для манифеста: русский раздел + переводы (если есть) с отметками языка. Возвращает (текст, дата)."""
    body, date = changelog_section(read(changelog), version)
    check_section(body, date, version, changelog)
    parts = [body]
    base = os.path.dirname(changelog)
    for lang, name in LANG_FILES.items():
        path = os.path.join(base, name)
        if not os.path.exists(path):
            continue
        try:
            tbody, tdate = changelog_section(read(path), version)
        except Fail:
            continue  # перевода этой версии нет — покажется русский текст
        check_section(tbody, tdate, version, name)
        if tdate != date:
            raise Fail(f"{name}: дата версии {version} ({tdate}) не совпадает с CHANGELOG.md ({date})")
        parts.append(f"<!-- lang: {lang} -->\n{tbody}")
    notes = "\n\n".join(parts).strip() + "\n"
    if len(notes) > MAX_NOTES:
        raise Fail(f"описание изменений слишком длинное ({len(notes)} символов, не больше {MAX_NOTES})")
    return notes, date


# ---------- Ключ и конфигурация ----------

def config_updater(config):
    conf = json.loads(read(config))
    u = conf.get("plugins", {}).get("updater")
    if not isinstance(u, dict):
        raise Fail(f"{config}: нет plugins.updater")
    return conf, u


def decode_pubkey(b64):
    """Открытый ключ minisign из base64 (как в tauri.conf.json): (id ключа, 32 байта ключа Ed25519)."""
    try:
        text = base64.b64decode(b64, validate=True).decode("utf-8")
    except Exception:
        raise Fail("открытый ключ — не base64 текста ключа minisign")
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 2 or not lines[0].startswith("untrusted comment:"):
        raise Fail("открытый ключ: неверный формат minisign")
    raw = base64.b64decode(lines[1])
    if len(raw) != 42 or raw[:2] != b"Ed":
        raise Fail("открытый ключ: ожидался ключ Ed25519 minisign (42 байта)")
    return raw[2:10], raw[10:]


def check_pubkey(config, repo=None):
    conf, u = config_updater(config)
    key = u.get("pubkey", "")
    if not key or key == PLACEHOLDER:
        raise Fail(f"{config}: открытый ключ обновлений не задан (заглушка {PLACEHOLDER}) — см. UGH-RELEASE-GUIDE.md, шаг «Ключ подписи»")
    kid, _ = decode_pubkey(key)
    if u.get("requireSignedVersion") is not True:
        raise Fail("plugins.updater.requireSignedVersion должен быть true")
    for bad in ("dangerousInsecureTransportProtocol", "dangerousAcceptInvalidCerts", "dangerousAcceptInvalidHostnames", "allowDowngrades"):
        if u.get(bad):
            raise Fail(f"plugins.updater.{bad} включён — так публиковать нельзя")
    eps = u.get("endpoints") or []
    if not eps or any(not str(e).startswith("https://") for e in eps):
        raise Fail("plugins.updater.endpoints: нужен хотя бы один адрес, только https://")
    if repo:
        want = f"https://github.com/{repo}/releases/latest/download/latest.json"
        if eps[0] != want:
            raise Fail(f"адрес обновлений {eps[0]} не совпадает с репозиторием релизов ({want})")
    return kid.hex().upper()


# ---------- Подпись minisign ----------

def verify_signature(data, sig_b64, pubkey_b64, version=None):
    """Проверка подписи Tauri (minisign): подпись файла и подпись доверенного комментария.
    Возвращает доверенный комментарий. С version — комментарий обязан содержать version:<version>."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    kid, pk = decode_pubkey(pubkey_b64)
    try:
        text = base64.b64decode(sig_b64.strip(), validate=True).decode("utf-8")
    except Exception:
        raise Fail("подпись — не base64 текста подписи minisign (нужно содержимое .sig)")
    lines = text.splitlines()
    if len(lines) < 4 or not lines[0].startswith("untrusted comment:") or not lines[2].startswith("trusted comment: "):
        raise Fail("подпись: неверный формат minisign")
    raw = base64.b64decode(lines[1])
    if len(raw) != 74:
        raise Fail("подпись: неверная длина")
    alg, sig_kid, sig = raw[:2], raw[2:10], raw[10:]
    if sig_kid != kid:
        raise Fail(f"подпись сделана другим ключом (id {sig_kid.hex().upper()}, ожидался {kid.hex().upper()})")
    if alg == b"ED":
        message = hashlib.blake2b(data, digest_size=64).digest()
    elif alg == b"Ed":
        message = data
    else:
        raise Fail("подпись: неизвестный алгоритм")
    key = Ed25519PublicKey.from_public_bytes(pk)
    try:
        key.verify(sig, message)
    except InvalidSignature:
        raise Fail("подпись файла неверна: файл изменён или подписан другим ключом")
    trusted = lines[2][len("trusted comment: "):]
    try:
        key.verify(base64.b64decode(lines[3]), sig + trusted.encode("utf-8"))
    except InvalidSignature:
        raise Fail("подпись доверенного комментария неверна")
    if version is not None:
        fields = dict(f.split(":", 1) for f in trusted.split("\t") if ":" in f)
        if fields.get("version") != version:
            raise Fail(f"подпись привязана к версии {fields.get('version')!r}, ожидалась {version} (подписывать с --app-version)")
    return trusted


# ---------- Манифест ----------

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


LOCAL_HTTP = re.compile(r"^http://(127\.0\.0\.1|localhost)(:\d+)?/")


def url_ok(url, allow_local_http=False):
    return url.startswith("https://") or (allow_local_http and bool(LOCAL_HTTP.match(url)))


def build_manifest(version, file, sig, url, notes, pub_date, allow_local_http=False):
    if not SEMVER.match(version):
        raise Fail(f"версия {version!r} не X.Y.Z")
    if not url_ok(url, allow_local_http):
        raise Fail("адрес файла обновления должен быть https://")
    return {
        "version": version,
        "notes": notes,
        "pub_date": pub_date,
        "platforms": {
            PLATFORM: {
                "url": url,
                "signature": read(sig).strip(),
                # дополнительные поля UGH: размер для окна обновления и SHA-256 для проверки в CI (плагин их не читает)
                "size": os.path.getsize(file),
                "sha256": sha256(file),
            }
        },
    }


def verify_manifest(m, file, version, pubkey, repo=None, url=None, exe=True, allow_local_http=False):
    """Проверки манифеста в том виде, в каком его читает плагин (все поля), и файла по нему."""
    if not isinstance(m, dict):
        raise Fail("манифест — не объект JSON")
    if m.get("version") != version or not SEMVER.match(str(m.get("version"))):
        raise Fail(f"версия в манифесте {m.get('version')!r}, ожидалась {version}")
    if "pub_date" in m:
        try:
            dt.datetime.fromisoformat(str(m["pub_date"]).replace("Z", "+00:00"))
        except ValueError:
            raise Fail(f"pub_date не RFC 3339: {m['pub_date']!r}")
        if not str(m["pub_date"]).endswith("Z"):
            raise Fail("pub_date должен быть в UTC (…Z)")
    notes = m.get("notes")
    if not isinstance(notes, str) or not notes.strip():
        raise Fail("в манифесте нет описания изменений (notes)")
    plats = m.get("platforms")
    if not isinstance(plats, dict) or set(plats) != {PLATFORM}:
        raise Fail(f"platforms должен содержать только {PLATFORM}")
    p = plats[PLATFORM]
    for k in ("url", "signature"):
        if not isinstance(p.get(k), str) or not p[k]:
            raise Fail(f"platforms.{PLATFORM}.{k} отсутствует")
    if not url_ok(p["url"], allow_local_http):
        raise Fail("адрес файла обновления — не https://")
    if repo and not p["url"].startswith(f"https://github.com/{repo}/releases/download/v{version}/"):
        raise Fail(f"адрес файла {p['url']} не указывает на релиз v{version} в {repo}")
    if url and p["url"] != url:
        raise Fail(f"адрес файла {p['url']}, ожидался {url}")
    size = os.path.getsize(file)
    if p.get("size") != size:
        raise Fail(f"размер в манифесте {p.get('size')}, у файла {size}")
    if p.get("sha256") != sha256(file):
        raise Fail("SHA-256 файла не совпадает с манифестом")
    with open(file, "rb") as f:
        data = f.read()
    if exe and data[:2] != b"MZ":
        raise Fail("файл обновления — не исполняемый файл Windows (нет MZ)")
    return verify_signature(data, p["signature"], pubkey, version)


# ---------- Ключ внутри собранной программы ----------

MAIN_EXE = "ultimate-gaming-hub.exe"


def find_7z():
    for name in ("7z", "7za"):
        p = shutil.which(name)
        if p:
            return p
    p = r"C:\Program Files\7-Zip\7z.exe"
    return p if os.path.exists(p) else None


def embedded_strings(nsis, needles):
    """Достаёт exe программы из NSIS-пакета (7-Zip) и проверяет, что в нём есть каждая строка.
    Конфигурация Tauri (в т.ч. plugins.updater.pubkey и endpoints) встраивается в exe как текст."""
    z = find_7z()
    if not z:
        raise Fail("не найден 7-Zip (7z) — нужен, чтобы проверить exe внутри NSIS-пакета")
    with tempfile.TemporaryDirectory(prefix="ugh-embedded-") as d:
        r = subprocess.run([z, "e", "-y", f"-o{d}", nsis, MAIN_EXE], capture_output=True, text=True, timeout=300)
        exe = os.path.join(d, MAIN_EXE)
        if r.returncode != 0 or not os.path.isfile(exe):
            raise Fail(f"в NSIS-пакете не найден {MAIN_EXE} (7z: код {r.returncode})")
        with open(exe, "rb") as f:
            data = f.read()
    if data[:2] != b"MZ":
        raise Fail(f"{MAIN_EXE} — не исполняемый файл Windows")
    missing = [n for n in needles if n.encode("utf-8") not in data]
    if missing:
        raise Fail("в программе внутри пакета нет ожидаемых строк: " + ", ".join(m[:24] + "…" for m in missing))
    return len(data)


# ---------- Пре-релиз против кандидата ----------

def release_names(version):
    upd = f"UltimateGamingHub_{version}_x64-update.exe"
    return {
        "setup": f"ALLIANCE_{version}_x64-setup.exe",
        "update": upd,
        "sig": upd + ".sig",
        "latest": "latest.json",
        "sums": "SHA256SUMS.txt",
    }


def parse_sums(text):
    out = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        m = re.fullmatch(r"([0-9a-f]{64}) [ *]?(.+)", line.strip())
        if not m:
            raise Fail(f"SHA256SUMS.txt: непонятная строка {line!r}")
        if m.group(2) in out:
            raise Fail(f"SHA256SUMS.txt: {m.group(2)} указан дважды")
        out[m.group(2)] = m.group(1)
    return out


def compare_release(rel, cand, version, pubkey, repo, assets, body=None):
    """Все файлы опубликованного пре-релиза (rel, скачаны анонимно) против кандидата сборки (cand).
    assets — список «имя размер» из API релиза. Любое расхождение — Fail."""
    n = release_names(version)
    want = set(n.values())
    # 1) состав и размеры файлов релиза (по данным GitHub) = скачанные файлы
    listed = {}
    for line in assets.splitlines():
        if line.strip():
            name, size = line.rsplit(" ", 1)
            listed[name] = int(size)
    if set(listed) != want:
        raise Fail(f"файлы пре-релиза не те: лишние {sorted(set(listed) - want)}, нет {sorted(want - set(listed))}")
    for name in want:
        p = os.path.join(rel, name)
        if not os.path.isfile(p):
            raise Fail(f"не скачан файл пре-релиза {name}")
        if os.path.getsize(p) != listed[name]:
            raise Fail(f"{name}: размер у GitHub {listed[name]}, скачано {os.path.getsize(p)}")
    # 2) установщик и файл обновления — байт в байт как в кандидате
    for key in ("setup", "update"):
        a, b = os.path.join(rel, n[key]), os.path.join(cand, n[key])
        if os.path.getsize(a) != os.path.getsize(b) or sha256(a) != sha256(b):
            raise Fail(f"{n[key]} в пре-релизе отличается от кандидата сборки")
    notes = read(os.path.join(cand, "RELEASE_NOTES.md"))
    # 3) latest.json: формат, адрес, размер/SHA = файл кандидата, подпись верна (закреплённый ключ) и = .sig
    with open(os.path.join(rel, n["latest"]), encoding="utf-8") as f:
        try:
            m = json.load(f)
        except json.JSONDecodeError as e:
            raise Fail(f"latest.json пре-релиза — не JSON: {e}")
    url = f"https://github.com/{repo}/releases/download/v{version}/{n['update']}"
    trusted = verify_manifest(m, os.path.join(cand, n["update"]), version, pubkey, repo, url)
    if m["platforms"][PLATFORM]["signature"] != read(os.path.join(rel, n["sig"])).strip():
        raise Fail("подпись в latest.json не совпадает с файлом .sig пре-релиза")
    if m.get("notes") != notes:
        raise Fail("описание изменений в latest.json не совпадает с RELEASE_NOTES.md кандидата")
    # 4) SHA256SUMS.txt пре-релиза: ровно эти файлы, хэши = фактические (RELEASE_NOTES.md — из кандидата)
    sums = parse_sums(read(os.path.join(rel, n["sums"])))
    expect = {n["setup"]: os.path.join(rel, n["setup"]), n["update"]: os.path.join(rel, n["update"]),
              n["sig"]: os.path.join(rel, n["sig"]), n["latest"]: os.path.join(rel, n["latest"]),
              "RELEASE_NOTES.md": os.path.join(cand, "RELEASE_NOTES.md")}
    if set(sums) != set(expect):
        raise Fail(f"SHA256SUMS.txt пре-релиза: ожидались {sorted(expect)}, есть {sorted(sums)}")
    for name, path in expect.items():
        if sums[name] != sha256(path):
            raise Fail(f"SHA256SUMS.txt пре-релиза: хэш {name} не совпадает")
    # 5) описание на странице релиза = RELEASE_NOTES.md
    if body is not None and body.replace("\r\n", "\n").strip() != notes.replace("\r\n", "\n").strip():
        raise Fail("описание на странице пре-релиза не совпадает с RELEASE_NOTES.md кандидата")
    return trusted


# ---------- Командная строка ----------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("notes")
    n.add_argument("--version", required=True)
    n.add_argument("--changelog", default="CHANGELOG.md")
    n.add_argument("--out")
    k = sub.add_parser("pubkey")
    k.add_argument("--config", default="src-tauri/tauri.conf.json")
    k.add_argument("--repo")
    mf = sub.add_parser("manifest")
    for a in ("--version", "--file", "--sig", "--url", "--notes", "--out"):
        mf.add_argument(a, required=True)
    mf.add_argument("--pub-date", help="RFC 3339 UTC; по умолчанию — дата из CHANGELOG.md, 12:00Z")
    mf.add_argument("--changelog", default="CHANGELOG.md")
    mf.add_argument("--allow-local-http", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("--manifest", required=True)
    v.add_argument("--file", required=True)
    v.add_argument("--version", required=True)
    g = v.add_mutually_exclusive_group()
    g.add_argument("--pubkey-file", help="файл .pub (base64), например временный ключ проверочной сборки")
    g.add_argument("--config", default="src-tauri/tauri.conf.json")
    v.add_argument("--repo")
    v.add_argument("--url")
    v.add_argument("--allow-local-http", action="store_true")
    e = sub.add_parser("embedded")
    e.add_argument("--nsis", required=True)
    ge = e.add_mutually_exclusive_group(required=True)
    ge.add_argument("--pubkey-file")
    ge.add_argument("--config")
    e.add_argument("--endpoint", action="append", default=[])
    cr = sub.add_parser("compare-release")
    for x in ("--release", "--candidate", "--version", "--pubkey-file", "--repo", "--assets"):
        cr.add_argument(x, required=True)
    cr.add_argument("--body")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "notes":
            if not SEMVER.match(a.version):
                raise Fail(f"версия {a.version!r} не X.Y.Z")
            notes, date = build_notes(a.version, a.changelog)
            if a.out:
                with open(a.out, "w", encoding="utf-8", newline="\n") as f:
                    f.write(notes)
            print(f"OK: описание изменений {a.version} от {date} — {len(notes)} символов")
        elif a.cmd == "pubkey":
            kid = check_pubkey(a.config, a.repo)
            print(f"OK: открытый ключ обновлений задан (id {kid}), адреса только https")
        elif a.cmd == "manifest":
            notes = read(a.notes)
            pub = a.pub_date
            if not pub:
                _, date = changelog_section(read(a.changelog), a.version)
                pub = f"{date}T12:00:00Z"
            m = build_manifest(a.version, a.file, a.sig, a.url, notes, pub, a.allow_local_http)
            with open(a.out, "w", encoding="utf-8", newline="\n") as f:
                json.dump(m, f, ensure_ascii=False, indent=2)
                f.write("\n")
            print(f"OK: {a.out} — версия {a.version}, {m['platforms'][PLATFORM]['size']} байт")
        elif a.cmd == "verify":
            with open(a.manifest, encoding="utf-8") as f:
                try:
                    m = json.load(f)
                except json.JSONDecodeError as e:
                    raise Fail(f"манифест — не JSON: {e}")
            if a.pubkey_file:
                pub = read(a.pubkey_file).strip()
            else:
                _, u = config_updater(a.config)
                pub = u.get("pubkey", "")
                if pub == PLACEHOLDER:
                    raise Fail("открытый ключ обновлений не задан (заглушка)")
            trusted = verify_manifest(m, a.file, a.version, pub, a.repo, a.url, allow_local_http=a.allow_local_http)
            print(f"OK: манифест и подпись верны ({trusted})")
        elif a.cmd == "embedded":
            if a.pubkey_file:
                pub = read(a.pubkey_file).strip()
            else:
                _, u = config_updater(a.config)
                pub = u.get("pubkey", "")
            if not pub or pub == PLACEHOLDER:
                raise Fail("открытый ключ не задан (заглушка)")
            decode_pubkey(pub)
            size = embedded_strings(a.nsis, [pub] + a.endpoint)
            print(f"OK: в {MAIN_EXE} ({size} байт) встроены ожидаемый открытый ключ" + (" и адрес обновлений" if a.endpoint else ""))
        elif a.cmd == "compare-release":
            pub = read(a.pubkey_file).strip()
            if not pub or pub == PLACEHOLDER:
                raise Fail("закреплённый ключ не задан (заглушка)")
            body = read(a.body) if a.body else None
            trusted = compare_release(a.release, a.candidate, a.version, pub, a.repo, read(a.assets), body)
            print(f"OK: все файлы пре-релиза v{a.version} совпадают с кандидатом, подпись верна ({trusted})")
    except Fail as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"ОШИБКА: нет файла {e.filename}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
