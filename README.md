# Ultimate Gaming Hub — релизы

Публичный репозиторий только для **готовых версий** Ultimate Gaming Hub: установщики и файлы обновления. Исходники здесь не хранятся.

- **Скачать программу:** раздел [Releases](../../releases/latest) → `UltimateGamingHub_<версия>_x64-setup.exe`.
- **Обновления:** программа (с версии 1.22.0) сама проверяет `releases/latest/download/latest.json`. Обновление ставится только после нажатия «Обновить» и только с верной цифровой подписью.

## Для владельца

Всё о настройке и выпуске — в `UGH-RELEASE-GUIDE.md` приватного репозитория.

| Путь | Назначение |
|---|---|
| `.github/workflows/publish.yml` | ручной workflow «Publish UGH release»: check / test / publish / withdraw |
| `release-config/updater.pub` | **закреплённый** открытый ключ обновлений: публикуется только сборка, в которую встроен именно он |
| `scripts/ugh_ci_updater.py` | проверка манифеста, подписи и ключа внутри программы (копия из приватного репозитория) |
| `tools/package*.json` | закреплённая версия Tauri CLI для подписи |

Секреты — только в Environments:
- `release-source`: `UGH_SOURCE_TOKEN`, только `main`;
- `release-signing`: ключ подписи, только `main`, **обязательное подтверждение**.

Секретов уровня репозитория нет.
