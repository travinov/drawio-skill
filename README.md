# Corporate Agent Skills for GigaCode CLI

Репозиторий содержит два независимых skill-based extension для GigaCode CLI:

| Extension | Версия | Назначение |
|---|---:|---|
| [`drawio-skill`](publish-drawio-skill/) | `1.24.0-corporate.4` | Редактируемые draw.io-диаграммы с безопасным Qwen command-input bridge, автономным bounded multi-model repair loop, строгим разделением working/publishable/final, потоковой трассировкой ролей, roadmap, git-flow и архитектурными схемами |
| [`bpmn-architect`](publish-bpmn-skill/) | `0.3.0` | Семантические BPMN 2.0 модели с многоуровневой раскладкой collaboration, spatial validation и round-trip проверкой |

Extension не вложены друг в друга и могут устанавливаться отдельно.

## Готовые архивы

- [`dist/drawio-skill-agent-extension.zip`](dist/drawio-skill-agent-extension.zip)
- [`dist/bpmn-architect-skill.zip`](dist/bpmn-architect-skill.zip)
- [`dist/SHA256SUMS.txt`](dist/SHA256SUMS.txt)

Предыдущая версия draw.io skill без агентного контура остаётся в ветке
[`main`](https://github.com/travinov/corporate-agent-skills/tree/main) и доступна
как [`drawio-skill-corporate.zip`](https://raw.githubusercontent.com/travinov/corporate-agent-skills/main/dist/drawio-skill-corporate.zip).
Непосредственный rollback для этой поставки — сохранённая агентная версия
`1.23.0-corporate.13` из backup, который создаёт установщик; команда отката
приведена внутри ZIP в
`docs/drawio-agent-extension-corporate-test-commands.md`.

Проверка архивов:

```bash
cd dist
shasum -a 256 -c SHA256SUMS.txt
```

## Установка в GigaCode CLI

Агентная версия Draw.io устанавливается как **extension**, а не копируется в
`~/.gigacode/skills`. Установщик использует корпоративный CLI
`/Users/travinov-sv/.gigacode/bin/gigacode`, проверяет полный внутренний
manifest распакованной поставки (а в режиме `--archive` также SHA-256 ZIP),
переносит прежний `~/.gigacode/skills/drawio-skill` в backup вне активных
каталогов, вызывает native `extensions install`, а `extensions validate`
запускает только в тех версиях GigaCode, где эта команда доступна.

Для корпоративного ноутбука без доступа к GitHub перенесите только ZIP,
распакуйте его в `Downloads` и запустите вложенный установщик:

```bash
cd ~/Downloads/drawio-skill
chmod +x install/*.sh
./install/install_drawio_agent_extension.sh
```

Скрипт автоматически определяет окружающую распакованную папку, поэтому ничего
не нужно скачивать с GitHub или копировать в `skills`. Установка зависимостей
может обратиться к Python registry, уже настроенному в корпоративной среде.

Проверка и откат:

```bash
./install/verify_drawio_agent_extension.sh
./install/rollback_drawio_agent_extension.sh --latest
```

После перезапуска GigaCode команда `/agents manage` должна показать четыре
`diagram-*` агента. Реальный запуск выполняется только на ноутбуке, где
установлен GigaCode CLI; локальные тесты репозитория используют fake CLI и не
изменяют `~/.gigacode`.

BPMN-пакет остается отдельным skill и устанавливается независимо:

```bash
mkdir -p ~/.gigacode/skills
unzip dist/bpmn-architect-skill.zip -d ~/.gigacode/skills
```

Зависимости BPMN extension:

```bash
cd ~/.gigacode/skills/bpmn-architect/scripts/corp-bpmn
npm ci
npm run self-check
```

## Схемы и валидация

Draw.io extension поставляет Draft 2020-12 schemas для roadmap и git-flow, source-aware проверку `.drawio`, детерминированную генерацию и real-export smoke check.

BPMN extension поставляет отдельные JSON Schema 2020-12 для v1 single-process и v2 collaboration, capability matrix, fail-closed validation и semantic round-trip через `bpmn-moddle`.

## Воспроизводимая сборка

```bash
python3 scripts/release_skills.py all --registry
```

Команда проверяет зависимости, собирает два детерминированных ZIP, создаёт manifests и checksums, распаковывает архивы в чистые временные каталоги и запускает тесты уже из распакованной поставки.

Подробности: [`release/README.md`](release/README.md).
