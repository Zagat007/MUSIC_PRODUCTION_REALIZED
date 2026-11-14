# Suno Release Master

Локальное офлайн‑приложение для Windows: анализ, бережный мастеринг, локальный ремонт дефектов,
организация альбомов и подготовка пакетов релиза для треков Suno (M4A/AAC, ALAC, WAV, FLAC, MP3).
Бесплатно: без аккаунтов, подписок, платных API и облака. Интеграция с ChatGPT (MCP) — необязательна.

**Важно.** WAV из AAC — это перекодирование, а не восстановление качества. Числа (LUFS, true peak)
— это техническая проверка, а не доказательство профессионального звучания: слушать обязательно.

## Быстрый старт (CLI)
```
srm scan "D:\Music\Suno" --project "D:\Music\Projects\collection"
srm analyze  --project "D:\Music\Projects\collection" --album "Album A"
srm propose  --project "..." --album "Album A" --mode safe_master
srm render   --project "..." --album "Album A"
srm preview  --project "..." --track-id 3 --version 1
srm approve  --project "..." --track-id 3 --version 1
srm signoff  --project "..." --track-id 3 --version 1 --note "A-B на мониторах"
srm export   --project "..." --album "Album A" --profile release_wav --confirm
srm validate --project "..." --album "Album A"
```
Все команды: `srm --help`. GUI: `srm-gui.exe` или `srm gui --project ...`.
MCP для ChatGPT: `srm serve-mcp --project ...` (см. `docs/OPENAI_INTEGRATION.md`).

## Документация
* `docs/ACCEPTANCE_RU.md` — актуальная приёмка: 73 теста и проверка Windows-сборок
* `docs/USER_GUIDE_RU.md` — руководство пользователя
* `docs/ARCHITECTURE.md`, `docs/DSP_DECISIONS.md`, `docs/AUDIO_VALIDATION.md`
* `docs/RELEASE_PROFILES.md` — профили и проверенные требования площадок
* `docs/OPENAI_INTEGRATION.md`, `plugin/skill/` — ChatGPT
* `docs/LIMITATIONS.md` — что не работает или требует человека
* `LICENSE-THIRD-PARTY.md` — лицензии; `IMPLEMENTATION_LOG.md` — журнал разработки

## Разработка
```
scripts\setup_windows.ps1     # venv (Python 3.11) + зависимости
scripts\test_windows.ps1      # pytest с логом
scripts\build_windows.ps1     # PyInstaller: dist\srm\srm.exe, dist\srm-gui\srm-gui.exe
python scripts\verify_windows_build.py  # Проверка собранных программ и локальных команд плагина
```

Сборка сохраняет контрольные суммы исходников и программ в `dist/BUILD_INFO.json`.
Проверка сборки использует сгенерированный звук и записывает результат в `tests/qa/build_acceptance.json`.
