# drawio-skill extension

`drawio-skill extension` помогает GigaCode превращать текстовый запрос пользователя в редактируемую диаграмму `.drawio`, проверять ее структурно и экспортировать через draw.io Desktop CLI в PNG, SVG, PDF или JPG.

Extension состоит из агентных инструкций, reference-файлов и локальных Python-скриптов. В корпоративной сборке отключены внешние CDN для брендовых SVG-иконок: используются локальные draw.io shapes и встроенные генераторы.

## Как работает

1. **Diagram Intake Agent** анализирует запрос пользователя, определяет тип диаграммы и при необходимости задает короткие уточняющие вопросы.
2. Агент формирует `confirmed diagram brief`: тип диаграммы, цель, уровень детализации, layout, формат вывода и assumptions.
3. Skill выбирает подходящий путь генерации: Mermaid conversion, hand-written XML или один из bundled generators.
4. Скрипт создает `.drawio`.
5. `scripts/validate.py` проверяет структуру: dangling edges, duplicate ids, broken parents, overlaps и routing warnings.
6. draw.io Desktop CLI экспортирует preview/final изображения, если CLI доступен.

Intake не является жесткой анкетой. Он использует question matrix и задает только вопросы, которые реально улучшают диаграмму. Для сложных запросов последний вопрос свободный: например, нужно ли не разносить роли по дорожкам, а выделить их цветом, сгруппировать шаги по этапам или показать артефакты отдельными блоками.

## Какие диаграммы строит

- Flowchart и process diagram.
- Roadmap / product roadmap / project roadmap с вехами, зависимостями, baseline comparison и shift markers.
- sequence diagram через `scripts/seqlayout.py`.
- C4: Context / Container / Component через `scripts/c4.py`.
- ERD из SQL DDL через `scripts/sqlerd.py`.
- UML class / class hierarchy.
- Architecture / system / service diagrams.
- Git-flow и custom branch timeline через `scripts/gitflow.py`.
- Infrastructure diagrams из Terraform, Kubernetes и docker-compose.
- Import/dependency graphs для Python, JS/TS, Go, Rust.
- ML/DL model diagrams, network topology, mind maps и общие визуальные схемы.

## Пример запроса

```text
Мне нужно визуализировать custom git-flow работы команды с OpenSpec.
Это release-based процесс без develop.
Подготовь timeline-aware draw.io диаграмму: X = порядок шагов, Y = ветки.

Ветки:
- master - состояние микросервиса в пром.
- release/{release_number} - целевая релизная ветка микросервиса.
- spec/{jira_key} - ветка аналитика для подготовки change.md.
- feature/{jira_key} - ветка разработчика для реализации.

Процесс:
1) Аналитик создает spec/{jira_key} от release/{release_number}.
2) Аналитик готовит только change.md.
3) Аналитик создает PR spec/{jira_key} -> release/{release_number}.
4) Разработчик ревьювит и мержит PR.
5) В release/{release_number} появляется change.md в статусе "не реализовано".
6) Разработчик создает feature/{jira_key} от актуальной release/{release_number}.
7) Разработчик готовит design.md, tasks.md, код, тесты и обновляет master-spec.md.
8) Разработчик создает PR feature/{jira_key} -> release/{release_number}.
9) Другой разработчик ревьювит PR; аналитик проверяет смысл при необходимости.
10) PR мержится в release/{release_number}.
11) Тестировщик коммитит архивацию OpenSpec change в release/{release_number}.
12) После релизных проверок release/{release_number} продвигается в master.

Пожелание к виду: дорожки оставить по веткам, роли выделять цветом в подписях.
```

Ожидаемый результат: skill построит intermediate JSON-модель, запустит `gitflow_validate.py`, затем `gitflow.py`, проверит `.drawio` через `validate.py` и вернет `.drawio` плюс preview PNG, если draw.io CLI доступен.

## Формат таблицы для roadmap

Roadmap можно подать как Markdown-таблицу, CSV/XLSX-таблицу или вставку из табличного редактора. Агент нормализует строки в `roadmap.yaml`, затем строит `.drawio`. Минимально нужны `id`, `title`, `lane` и хотя бы одна дата: `start`/`end` для задачи или `milestone_date` для вехи.

| Колонка | Обязательная | Пример | Назначение |
|---|---:|---|---|
| `id` | да | `task-wallets` | Стабильный ID задачи или инициативы |
| `title` | да | `Wallet support` | Название задачи/инициативы |
| `lane` | желательно | `checkout` | Дорожка: продукт, команда, проект, стрим |
| `lane_title` | нет | `Checkout` | Человекочитаемое название дорожки |
| `start` | для task bar | `2026-07-01` | Начало работы |
| `end` | для task bar | `2026-09-30` | Конец работы |
| `milestone_id` | для вех | `m-wallet-pilot` | Стабильный ID вехи |
| `milestone` | для вех | `Wallet pilot` | Название вехи |
| `milestone_date` | для вех | `2026-09-30` | Текущая дата вехи |
| `baseline_milestone_date` | нет | `2026-09-15` | Дата этой же вехи в предыдущей версии roadmap |
| `shift_days` | нет | `15` | Явно заданное смещение; если пусто, считается из baseline/current дат |
| `shift_state` | нет | `delayed` | `delayed`, `accelerated`, `unchanged`, `added`, `removed` |
| `depends_on` | нет | `m-billing-api` | ID задачи/вехи, от которой зависит текущая строка |
| `dependency_type` | нет | `blocks` | `blocks`, `depends_on`, `influences`, `relates_to` |
| `impact` | нет | `high` | `low`, `medium`, `high` для связи или риска |
| `status` | нет | `at_risk` | `planned`, `in_progress`, `on_track`, `at_risk`, `blocked`, `done`, `cancelled` |
| `owner` | нет | `Payments` | Владелец задачи/вехи |
| `outcome_id` | нет | `outcome-fast-payments` | ID результата |
| `outcome` | нет | `Faster successful payments` | Бизнес-результат |
| `risk` | нет | `Vendor API delay` | Риск или причина смещения |
| `notes` | нет | `Pilot depends on billing API` | Дополнительная подпись/комментарий |

Пример:

```markdown
| id | title | lane | lane_title | start | end | milestone_id | milestone | milestone_date | baseline_milestone_date | shift_days | shift_state | depends_on | dependency_type | impact | status | owner | outcome_id | outcome | risk |
|---|---|---|---|---|---|---|---|---|---|---:|---|---|---|---|---|---|---|---|---|
| task-billing-api | Billing API hardening | billing | Billing | 2026-06-15 | 2026-08-01 | m-billing-api | Billing API stable | 2026-08-01 | 2026-08-15 | -14 | accelerated |  |  | medium | on_track | Billing | outcome-fast-payments | Faster successful payments |  |
| task-wallets | Wallet support | checkout | Checkout | 2026-07-01 | 2026-09-30 | m-wallet-pilot | Wallet pilot | 2026-09-30 | 2026-09-15 | 15 | delayed | m-billing-api | blocks | high | at_risk | Payments | outcome-fast-payments | Faster successful payments | Vendor certification delay |
| task-analytics | Analytics beta | checkout | Checkout | 2026-09-01 | 2026-10-15 | m-analytics-beta | Analytics beta | 2026-10-15 |  |  | added | m-wallet-pilot | influences | medium | planned | Analytics | outcome-insights | Pilot usage insights |  |
```

Правила смещения вех:

- Если `baseline_milestone_date` и `milestone_date` заполнены, skill сам считает `shift_days`.
- Если `shift_days` заполнен явно, он используется как подсказка и должен совпадать с датами, если даты тоже указаны.
- Если веха есть только в текущей таблице, укажите `shift_state=added` или оставьте baseline пустым.
- Если нужно показать удаленную веху, добавьте строку с `shift_state=removed`, заполните `baseline_milestone_date`, а `milestone_date` оставьте пустой.
- Для надежного сравнения версий сохраняйте одинаковые `milestone_id` между текущей и предыдущей дорожной картой.

## Установка в корпоративной среде

Распакуйте ZIP в каталог skills агента:

```bash
mkdir -p ~/.agents/skills
unzip drawio-skill-corporate.zip -d ~/.agents/skills
```

draw.io Desktop в корпоративной среде macOS/Windows устанавливается через внутренний маркетплейс SberUserSoft:

```text
https://sberusersoft.sigma.sbrf.ru/#search/Draw.io
```

Проверка CLI:

```bash
drawio --version
/Applications/draw.io.app/Contents/MacOS/draw.io --version
"C:\Program Files\draw.io\draw.io.exe" --version
```

Если draw.io установлен нестандартно, задайте путь:

```bash
export DRAWIO_BIN="/Applications/draw.io.app/Contents/MacOS/draw.io"
```

или создайте config:

```json
{
  "drawio_bin": "C:\\Program Files\\draw.io\\draw.io.exe"
}
```

Путь config:

- macOS/Linux/WSL: `~/.drawio-skill/config.json`
- Windows: `%USERPROFILE%\.drawio-skill\config.json`

## Основные компоненты

- `SKILL.md` - основная инструкция агенту: когда применять extension, как выбирать путь генерации, как экспортировать.
- `metadata.md` - карточка extension по корпоративному шаблону.
- `references/diagram-intake.md` - правила Diagram Intake Agent и матрица вопросов.
- `references/roadmap.md` - правила roadmap intake, `roadmap.yaml`, baseline comparison, shift markers и rendering.
- `references/git-flow.md` - правила git-flow/custom branch timeline.
- `references/xml-authoring.md`, `references/diagram-types.md`, `references/shapes.md` - правила ручного XML, типов диаграмм и shape lookup.
- `scripts/gitflow.py` - генератор timeline-aware git-flow/custom-flow диаграмм.
- `scripts/gitflow_validate.py` - валидатор входного JSON для git-flow/custom-flow.
- `scripts/roadmap.py` - генератор roadmap diagrams из `roadmap.yaml`.
- `scripts/roadmap_validate.py` - валидатор `roadmap.yaml`, refs, дат и baseline milestone deltas.
- `scripts/validate.py` - общий структурный lint для `.drawio`.
- `scripts/seqlayout.py`, `scripts/c4.py`, `scripts/sqlerd.py`, `scripts/autolayout.py` - специализированные генераторы.
- `tests/` - fixtures и unittest-проверки.

## Валидация и проверки

Проверить скрипты:

```bash
python3 -m py_compile scripts/gitflow.py scripts/gitflow_validate.py scripts/roadmap.py scripts/roadmap_validate.py
python3 -m unittest discover -s tests -p 'test_*.py'
```

Проверить git-flow генерацию:

```bash
python3 scripts/gitflow_validate.py tests/fixtures/gitflow/openspec_custom.json --strict
python3 scripts/gitflow.py tests/fixtures/gitflow/openspec_custom.json -o /tmp/openspec.drawio --route auto
python3 scripts/validate.py /tmp/openspec.drawio --strict
```

Проверить roadmap генерацию:

```bash
python3 scripts/roadmap_validate.py tests/fixtures/roadmap/baseline_shift.yaml
python3 scripts/roadmap.py tests/fixtures/roadmap/baseline_shift.yaml -o /tmp/roadmap.drawio
python3 scripts/validate.py /tmp/roadmap.drawio --strict
```

Если Graphviz `neato` недоступен, `--route auto` переключится на builtin routing. Это штатный режим для корпоративных ноутбуков без Graphviz.

## Ограничения

- Extension не использует внешние CDN для иконок.
- Git-flow генератор в v1 не парсит реальный `git log`, а строит диаграмму по JSON-модели, которую агент формирует из текста.
- Roadmap XML import в v1 поддерживает документированную нормализацию очевидных полей в `roadmap.yaml`; произвольные XML-схемы должны явно маппиться перед генерацией.
- Mermaid `gitGraph` не используется для timeline-aware git/custom flow, потому что не гарантирует стабильное размещение по timeline и branch lanes.
- Экспорт PNG/SVG/PDF требует draw.io Desktop CLI; без CLI можно сохранить `.drawio` и открыть его вручную.
