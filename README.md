# Corporate Agent Skills for GigaCode CLI

Репозиторий содержит два независимых skill-based extension для GigaCode CLI:

| Extension | Версия | Назначение |
|---|---:|---|
| [`drawio-skill`](publish-drawio-skill/) | `1.21.0-corporate.1` | Редактируемые draw.io-диаграммы, roadmap, git-flow, архитектурные и специализированные схемы |
| [`bpmn-architect`](publish-bpmn-skill/) | `0.3.0` | Семантические BPMN 2.0 модели с многоуровневой раскладкой collaboration, spatial validation и round-trip проверкой |

Extension не вложены друг в друга и могут устанавливаться отдельно.

## Готовые архивы

- [`dist/drawio-skill-corporate.zip`](dist/drawio-skill-corporate.zip)
- [`dist/bpmn-architect-skill.zip`](dist/bpmn-architect-skill.zip)
- [`dist/SHA256SUMS.txt`](dist/SHA256SUMS.txt)

Проверка архивов:

```bash
cd dist
shasum -a 256 -c SHA256SUMS.txt
```

## Установка в GigaCode CLI

```bash
mkdir -p ~/.gigacode/skills
unzip dist/drawio-skill-corporate.zip -d ~/.gigacode/skills
unzip dist/bpmn-architect-skill.zip -d ~/.gigacode/skills
```

Зависимости Draw.io extension:

```bash
cd ~/.gigacode/skills/drawio-skill
python3 -m pip install -r requirements.lock.txt
python3 scripts/self_check.py
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
