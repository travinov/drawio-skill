# drawio-skill extension

`drawio-skill extension` помогает GigaCode превращать текстовый запрос пользователя в редактируемую диаграмму `.drawio`, проверять ее структурно и экспортировать через draw.io Desktop CLI в PNG, SVG, PDF или JPG.

Extension состоит из агентных инструкций, reference-файлов и локальных Python-скриптов. В корпоративной сборке отключены внешние CDN для брендовых SVG-иконок: используются локальные draw.io shapes и встроенные генераторы.

## Как работает

1. **Diagram Intake Agent** анализирует запрос пользователя, определяет тип диаграммы и при необходимости задает короткие уточняющие вопросы.
2. Агент формирует `confirmed diagram brief`: тип диаграммы, цель, уровень детализации, layout, формат вывода и assumptions.
3. Skill выбирает подходящий путь генерации: Mermaid conversion, hand-written XML или один из bundled generators.
4. Скрипт создает `.drawio`.
5. `scripts/validate.py` проверяет структуру и layout: размеры, containment в container/swimlane, пересечение дорожек, text fit, прямые/waypoint-маршруты, terminal clearance и missing/shared pins в high-degree auto-routing.
6. draw.io Desktop CLI экспортирует preview/final изображения, если CLI доступен.

Для существующих `.drawio` доступен **Diagram Supervisor**: он импортирует
диаграмму в семантическую sidecar-модель, сопоставляет её с описанием
пользователя и только с теми справочными документами, которые пользователь передал явно,
применяет только локальные транзакционные
патчи и принимает следующую версию лишь как последовательное улучшение.
Supervisor хранит историю итераций, независимое review, решения пользователя и
SHA-256 receipt, доказывающий запуск строгой проверки именно для финального файла.
Продуктовый runtime не ищет и не выбирает OpenSpec автоматически.
Каждый новый mutable-run ведёт строгий v2 control plane в
`.diagram-runs/<run-id>/lifecycle-v2`: immutable source bundle, DiagramSpec,
implementation snapshot, state, checkpoint, decision, semantic approval,
validation receipt, Reviewer verdict и publication transaction связаны
хешами в `run-manifest.v2.jsonl`. Старые v1-запуски доступны для read-only
trace/manual handoff, но не возобновляются и не изменяются.

Проверка существующего файла в корпоративном GigaCode запускается одной
командой из каталога проекта:

```text
/drawio:review
```

Команда сама создаёт `.diagram-runs/<run-id>`, выполняет host-preflight,
строгую валидацию и запускает Independent Reviewer через отдельный процесс с
моделью из `data/model-routing.default.json`. Подбирать в чате формулировку для
запуска инструментов не требуется. Исходный `.drawio` эта команда не изменяет.

Полный агентный цикл запускается командами:

```text
/drawio:create "Создай диаграмму процесса обработки заказа"
/drawio:improve
/drawio:improve "Добавь новые данные и исправь найденные недостатки"
/drawio:resume continue "Уточнение или замечание"
/drawio:resume approve
/drawio:trace
```

Qwen передаёт `{{args}}` custom-команды как одно shell-escaped значение. Все
пять `/drawio:*` entrypoint сохраняют его в `DRAWIO_COMMAND_ARGS`, после чего
`command_ux.py` безопасно разбирает кавычки и флаги через `shlex.split` без
`eval`. Поэтому одинаково поддерживаются разговорная форма, `--diagram` /
`--request`, `--run` / `--decision` / `--feedback`, пути с пробелами и один
ведущий `@` у `.drawio` file reference. Пользователь не может переопределить
host-owned `--workspace` или `--cli`.

Рабочий каталог определяет проект. `create` сам выбирает незанятое имя файла;
пустой `improve` продолжает последний завершённый read-only review, если SHA-256
диаграммы не изменился, иначе автоматически выбирает единственный `.drawio` в
корне проекта и использует стандартную задачу исправления замечаний; `review`
также выбирает единственный файл. `/drawio:resume` без `--run` выбирает
последний pending run по детерминированной host policy;
`/drawio:resume --run <run-id>` выбирает указанный run. Если pending run
отсутствует, команда возвращает ошибку. Возобновление продолжает сохранённое
состояние run и не повторяет уже пройденные intake-вопросы.
Прежние `--diagram`, `--request`, `--run`, `--decision` и `--feedback`
сохраняются как расширенный интерфейс для автоматизации.

`create` и `improve` действительно запускают изолированные Supervisor,
Semantic Analyst, Repair и Reviewer по состоянию процесса. Каждая следующая
итерация строится от последнего монотонно улучшенного working-кандидата;
отклонённый файл остаётся только evidence. Host сам выполняет до четырёх
итераций, передаёт Repair машинные замечания Validator и один раз повторяет
исправимую ошибку с той же нормализованной сигнатурой. Если строгий результат
недостижим, но сохранённая диаграмма структурно и семантически безопасна, Host
завершает run как `best_effort_completed` и возвращает файл с оставшимися
layout/readability findings. Пользователь нужен при неоднозначном semantic
conflict, нарушении целостности evidence, небезопасном degraded output и final
acceptance — команда `continue` после каждой обычной попытки не требуется.
`trace` показывает цепочку ролей, фактические модели,
инструменты, обе версии event ledger, validation receipts, implementation
snapshot, решения пользователя и journal публикации. Он заново выводит
фактическую модель из сохранённого raw runtime output и сверяет её с локальной
routing policy, а не доверяет полям manifest. Это локальная проверка evidence,
не внешняя криптографическая аттестация против пользователя, способного
переписать все файлы run целиком.

Roadmap использует v1 для baseline comparison и v2 для полной истории
переносов; git-flow остается v1. Оба профиля имеют дополнительный
source-aware gate: итоговый XML сверяется с исходной моделью по тексту,
координатам, дорожкам и обязательным связям.

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

## Установка в корпоративной среде

Полная русская инструкция по установке, проверке моделей, review,
trace, improve и сбору evidence включена в архив:
[`docs/drawio-agent-extension-corporate-test-commands.md`](docs/drawio-agent-extension-corporate-test-commands.md).

Папка содержит native-compatible manifest `gemini-extension.json`, четыре
описания в `agents/*.md` и автономный каталог `install/`. После переноса ZIP на
корпоративный Mac распакуйте его в `Downloads` и выполните без обращения к
GitHub:

```bash
cd ~/Downloads/drawio-skill
chmod +x install/*.sh
./install/install_drawio_agent_extension.sh
```

После перезапуска откройте `/agents manage`: раздел extension agents должен показать `diagram-supervisor`,
`diagram-reviewer`, `diagram-repair` и `diagram-semantic-analyst`. В GigaCode
используйте эквивалентные команды extension/agents конкретной сборки. Проверенная
корпоративная сборка GigaCode 26.5.17 (Qwen Code 0.13.1) запускает extension
agents, но их видимость сама по себе не доказывает модель. Поэтому все четыре
роли lifecycle-команд запускаются через isolated `gigacode --model ...`;
глобальный `/model` не переключается. Подробности и доказательство модели:
`references/model-routing.md`.

Изолированный процесс дополнительно запускается без extension-контекста, с
непустым sentinel-allowlist, который убирает все core tools из registry модели,
с пустым `--allowed-mcp-server-names`, который до discovery убирает глобально
настроенные Jira/Bitbucket/другие MCP servers, с резервным deny-list,
`--approval-mode default` и ограничением числа ходов. Установщик требует этот
CLI-флаг и прекращает установку до запуска роли, если corporate fork его не
поддерживает.
Plan mode здесь намеренно не используется: в Qwen Code 0.13.1 он требует
завершить ответ через недоступный `exit_plan_mode` и может привести к
`FatalTurnLimitedError`. Default approval не открывает инструменты, поскольку
sentinel-allowlist оставляет registry пустым.
Если в `runtime-output.json` / `runtime-output.jsonl` всё же появился `tool_use`, `diagram-*` agent или
`drawio:*` command, роль завершается ошибкой до запуска следующего агента. Это
защищает от рекурсивного вызова `diagram-supervisor` и циклов `list_directory`.
При поддержке `stream-json` события сохраняются построчно, поэтому при ненулевом
exit code остаётся частичная трасса модели. Сохраняются runtime capture и редактированный
`runtime-stderr.txt`; `/drawio:trace` показывает их целостность и isolation proof.
Если основной Supervisor (`GigaChat-3-Ultra`) получает подтверждённый
`FatalTurnLimitedError` без утечки инструментов, host один раз запускает
Supervisor на `vllm/DeepSeek-V4-Flash-262k`. Если Repair на
`vllm/MiniMax-M3-113k` подтверждённо завис, исчерпал ходы, недоступен или не
вернул ответ, Host один раз повторяет тот же hash-bound input на
`vllm/Qwen3.6-35B-262k`. Оба запуска остаются в evidence, а
`host-result.json` показывает `model_diversity_degraded: true`. Нарушения
изоляции и integrity не повторяются. Исправимые output-contract/scope/tool
ошибки Repair/Reviewer получают один bounded orchestration retry с отдельным
`internal-feedback-*.json`; повтор той же сигнатуры приводит к plateau.

В этой корпоративной сборке deterministic command host является хостом и
исполнителем Supervisor-процесса. Он не передаёт весь цикл native
`diagram-supervisor`: сначала сам выполняет `host-preflight`, затем запускает
детерминированные проверки и только отдельные Reviewer/Repair/Semantic Analyst
через isolated CLI. Доказательства находятся в проекте в
`.diagram-runs/<run-id>/host-preflight.json`, `run-manifest.jsonl` и
`lifecycle-v2/run-manifest.v2.jsonl`.

Поле Supervisor `required_roles` теперь является рекомендацией модели, а не
способом случайно отключить часть жизненного цикла. Host сохраняет его как
`supervisor_declared_roles`, отдельно записывает `host_mandatory_roles` и
исполняет их объединение `required_roles`. Для первого create/improve это
Supervisor, Semantic Analyst, Repair и Reviewer; для продолжения — Supervisor,
Repair и Reviewer. Repair запускается только при замечаниях валидатора или
Reviewer. Несовместимое с фазой действие и остальные evidence-гейты остаются
fail-closed.

Reviewer получает строгий `reviewer-input.v2` и возвращает только
`reviewer-analysis.v2`: решение и структурированные findings. Host формирует
`reviewer-verdict.v2` из фактических candidate/report/receipt/source/semantic
SHA и проверенного model/isolation proof; модель не задаёт эти значения. Если
v2-ответ нарушил только output schema или cross-field contract, допускается
одна correction-попытка той же ролью и моделью с неизменным input hash. Ошибки
model proof, isolation, tool use, capability, timeout и integrity не повторяются.
Read-only review содержит `workflow.json`, поэтому `/drawio:trace` выбирает
свежий review, а не более старый improve.

Semantic Analyst v2 также не вычисляет evidence SHA или `operation_id`: Qwen
возвращает строгий page-scoped `semantic-analysis.v2` с nodes, edges, parents,
style hints, pins и waypoints. Host связывает его с фактическим source bundle и
baseline, затем детерминированно строит canonical `semantic-plan.v2` и typed
delta с add/remove/update/relationship/parent operations.

Raw `roles/repair-*/output.json` остаётся неизменным модельным evidence. Перед
применением Host создаёт рядом `host-bound.patch.json`, подставляет SHA и
semantic digest фактического working baseline, сужает affected region и
отклоняет операции вне разрешённых targets/operation types.

Пользовательский `continue` не начинает задачу заново. Непустой комментарий
становится новой hash-bound `confirmed_clarification`. Однозначный layout-only
комментарий (например, исправить Manhattan-маршрут только `e-2`) сразу
превращается в узкий host-owned repair scope и не запускает Semantic Analyst.
Смешанный либо семантический комментарий сверяет Semantic Analyst с тем же
working-артефактом. Решение
идемпотентно связано с checkpoint. `approve_with_findings` возможно только для
integrity-valid результата со strict pass, Reviewer approve и только
warning/info findings; error findings запрещены. Публикация использует
journaled no-clobber для create и compare-and-swap с rollback copy для improve.

Workflow различает три уровня: `working_artifact` — лучший монотонный baseline,
который ещё может не проходить strict; `publishable_candidate` — exact artifact
со strict receipt и Reviewer approve; `final_artifact`/`published_artifact` —
только результат явного финального решения пользователя. Legacy `accepted_*`
остаются внутренним compatibility mirror и не выдаются как финальный успех.

Renderer registry выбирается по валидированному `diagram_type`. Roadmap,
git-flow и C4 используют существующий специализированный генератор только при
наличии schema-valid документа, явно переданного пользователем и уже связанного
с immutable source bundle. При отсутствии такого source model Host честно
фиксирует причину и использует generic adapter; он не выдумывает данные
специализированного формата. Любой adapter выдаёт лишь candidate и проходит
общие validation, Reviewer, checkpoint, trace и publication gates.
Для явно переданного roadmap, git-flow или C4 JSON/YAML используйте расширенную
форму `/drawio:create --renderer-source "path/to/source.json" --request "..."`.
Обычная разговорная команда остаётся без изменений и не ищет
специализированные источники в рабочем каталоге.

После перезапуска GigaCode проверяйте работу расширения так:

```text
/drawio:review
```

В конце ответ содержит `run_dir`, состояние валидации, verdict Reviewer,
`requested_model`, `resolved_model` и `model_proof`. Эти данные берутся из
артефактов запуска, а не из `/stats model` основной сессии.

Установщик по умолчанию использует следующие пути корпоративного ноутбука:

```text
/Users/travinov-sv/.gigacode/bin/gigacode
/Users/travinov-sv/.gigacode/skills
/Users/travinov-sv/.gigacode/extensions
```

Перед установкой он сверяет полный внутренний manifest распакованной поставки и
обязательные файлы, запускает native `gigacode extensions validate`, если эта
команда доступна в установленной версии CLI, сохраняет предыдущий skill/extension в
`~/.gigacode/backups/drawio-agent-extension`, убирает конфликтующий legacy
`drawio-skill` из активного `skills` и вызывает native `extensions install`.
Вложенный установщик автоматически использует окружающую распакованную папку;
дополнительные файлы и GitHub не нужны. Откат:
`./install/rollback_drawio_agent_extension.sh --latest`.

В corporate mode команда `gigacode extensions update` отключена. Для обновления
перенесите новый ZIP, распакуйте его и повторно запустите вложенный установщик.
Предыдущую версию вручную удалять не нужно: установщик сначала сохранит её в
backup, затем переустановит extension из локального approved source.

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
- `assets/roadmap/roadmap-template.xlsx` - канонический intake с формулами переносов; CSV рядом — data-only fallback.
- `scripts/roadmap_template.py`, `scripts/roadmap_table.py` - копирование шаблона и импорт рабочей копии в v2 YAML.
- `scripts/roadmap_validate.py` - валидатор `roadmap.yaml`, refs, дат и baseline milestone deltas.
- `scripts/validate.py` - общий structural/layout lint для `.drawio`; relaxed-режим возвращает layout warnings, а `--strict` делает их блокирующими. Auto-routed orthogonal/ELK bends не угадываются: для связанных hub-узлов требуются уникальные распределённые `entryX/exitX`, после XML-проверки обязателен PNG export smoke.
- `references/diagram-supervisor.md`, `scripts/diagram_supervisor.py` - host-owned resumable agent/tool loop для существующих диаграмм: preflight, `DiagramSpec`, patch-only repair, orthogonal waypoints, monotonic comparison, receipts, human review и manual handoff.
- `references/model-routing.md`, `scripts/agent_runtime.py`, `agents/*.md` - native и isolated per-role model routing; stock Gemini orchestration выполняет main host, потому что subagent recursion запрещён.
- `scripts/seqlayout.py`, `scripts/c4.py`, `scripts/sqlerd.py`, `scripts/autolayout.py` - специализированные генераторы.
- `tests/` - fixtures и unittest-проверки.

## Валидация и проверки

Установить объявленные зависимости и проверить окружение:

```bash
python3 -m pip install -r requirements.txt
python3 scripts/self_check.py --check-registry
```

Поддерживаются `PyYAML>=6.0,<7`, `jsonschema>=4.18,<5` и `openpyxl>=3.1,<4`. Проверка registry
использует только уже настроенный pip source и временный
`pip download --no-deps`; в текущее Python-окружение ничего не устанавливает.

Проверить скрипты:

```bash
python3 -m py_compile scripts/gitflow.py scripts/gitflow_validate.py scripts/roadmap.py scripts/roadmap_validate.py
python3 -m unittest discover -s tests -p 'test_*.py'
```

Проверить git-flow генерацию:

```bash
python3 scripts/gitflow_validate.py tests/fixtures/gitflow/openspec_custom.json --strict
python3 scripts/gitflow.py tests/fixtures/gitflow/openspec_custom.json -o /tmp/openspec.drawio --route builtin
python3 scripts/validate.py /tmp/openspec.drawio --profile gitflow --source tests/fixtures/gitflow/openspec_custom.json --strict
python3 scripts/verify_determinism.py gitflow tests/fixtures/gitflow/openspec_custom.json --route builtin
```

Проверить roadmap генерацию:

```bash
python3 scripts/roadmap_validate.py tests/fixtures/roadmap/baseline_shift.yaml
python3 scripts/roadmap.py tests/fixtures/roadmap/baseline_shift.yaml -o /tmp/roadmap.drawio
python3 scripts/validate.py /tmp/roadmap.drawio --profile roadmap --source tests/fixtures/roadmap/baseline_shift.yaml --strict
python3 scripts/verify_determinism.py roadmap tests/fixtures/roadmap/baseline_shift.yaml
python3 scripts/export_smoke.py /tmp/roadmap.drawio -o /tmp/roadmap.png
```

Если Graphviz `neato` недоступен, `--route auto` переключится на builtin routing. Это штатный режим для корпоративных ноутбуков без Graphviz.

## Ограничения

- Extension не использует внешние CDN для иконок.
- Git-flow генератор в v1 не парсит реальный `git log`, а строит диаграмму по JSON-модели, которую агент формирует из текста.
- Roadmap XML import в v1 поддерживает документированную нормализацию очевидных полей в `roadmap.yaml`; произвольные XML-схемы должны явно маппиться перед генерацией.
- Mermaid `gitGraph` не используется для timeline-aware git/custom flow, потому что не гарантирует стабильное размещение по timeline и branch lanes.
- Экспорт PNG/SVG/PDF требует draw.io Desktop CLI; без CLI можно сохранить `.drawio` и открыть его вручную.
