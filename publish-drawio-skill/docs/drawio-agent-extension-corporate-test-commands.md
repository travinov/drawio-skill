# Draw.io Agent Extension: установка и проверка в GigaCode

Версия: `1.24.0-corporate.4`

В этой версии Host ведёт bounded autonomous repair-loop, передаёт Repair
машинные замечания, исполняет только отдельный host-bound patch, различает
working/publishable/final artifacts и не просит `continue` после каждой
исправимой попытки. Strict-failed кандидат не запускает Reviewer и не может
быть опубликован.

Эта инструкция уже включена в `drawio-skill-agent-extension.zip` и после
распаковки находится в
`drawio-skill/docs/drawio-agent-extension-corporate-test-commands.md`.

Контрольная сумма самого ZIP публикуется отдельно в
`drawio-skill-agent-extension.zip.sha256` или в сообщении о релизе. Её
нельзя зашить внутрь проверяемого ZIP: это изменило бы его
содержимое и саму контрольную сумму.

Каждый блок ниже содержит одну строку для целиком копирования.
Команды с `!` выполняются внутри GigaCode; остальные shell-команды —
в обычном Terminal.

## 1. Terminal: проверить SHA-256

```bash
cd "$HOME/Downloads" && shasum -a 256 drawio-skill-agent-extension.zip
```

Сравните вывод с внешней контрольной суммой из
`drawio-skill-agent-extension.zip.sha256` или сообщения о релизе.

## 2. Terminal: распаковать, установить и проверить

Перед запуском остановите текущую сессию GigaCode через `Ctrl+C`.

```bash
cd "$HOME/Downloads" || exit 1; if [ -d drawio-skill ]; then mv drawio-skill "drawio-skill-old-$(date +%Y%m%d-%H%M%S)"; fi; /usr/bin/ditto -x -k drawio-skill-agent-extension.zip . && bash "$HOME/Downloads/drawio-skill/install/install_drawio_agent_extension.sh" && bash "$HOME/Downloads/drawio-skill/install/verify_drawio_agent_extension.sh"
```

## 3. Terminal: проверить активную версию

```bash
sed -n '1,20p' "$HOME/.gigacode/extensions/publish-drawio-skill/gemini-extension.json"
```

Ожидаемо: `"version": "1.24.0-corporate.4"`.

## 4. Terminal: запустить GigaCode из каталога проекта

```bash
cd "/Users/travinov-sv/Documents/DrawioTest" && "$HOME/.gigacode/bin/gigacode"
```

В GigaCode выберите через `/model` базовую модель `vllm/MiniMax-M3-113k`.

## 5. GigaCode: проверить cwd

```text
!pwd; ls -la
```

Ожидаемо: `/Users/travinov-sv/Documents/DrawioTest`.

## 6. GigaCode: проверить версию extension

```text
!sed -n '1,20p' "$HOME/.gigacode/extensions/publish-drawio-skill/gemini-extension.json"
```

## 7. GigaCode: проверить флаги headless CLI

```text
!"$HOME/.gigacode/bin/gigacode" --help | grep -E -- '--model|--extensions|--system-prompt|--max-session-turns|--core-tools|--allowed-mcp-server-names|--exclude-tools|--output-format|stream-json|--approval-mode'
```

Нужно увидеть `--model`, `--extensions`, `--system-prompt`,
`--max-session-turns`, `--core-tools`, `--allowed-mcp-server-names`,
`--exclude-tools`, `--output-format`, `stream-json` и `--approval-mode`. Если
`--allowed-mcp-server-names` отсутствует, verifier должен остановить
установку: запускать роли без этого барьера нельзя.

Проверить установленную команду изоляции:

```text
!grep -n -- 'allowed-mcp-server-names\|allowed_mcp_servers' "$HOME/.gigacode/extensions/publish-drawio-skill/scripts/agent_runtime.py"
```

В коде должны присутствовать `"--allowed-mcp-server-names", ""` и evidence
`"allowed_mcp_servers": []`.

## 8. GigaCode: очистить каталог изолированной пробы

```text
!rm -rf "$PWD/.gigacode-probe"; mkdir -p "$PWD/.gigacode-probe"
```

## 9. GigaCode: найти существующий вход Reviewer

```text
!find "$PWD/.diagram-runs" -type f -name 'reviewer-audit-input.v2.json' | tail -5
```

Если файлы не найдены, сначала выполните команду из раздела 10.

## 10. GigaCode: создать вход Reviewer, если его нет

```text
/drawio:review "/Users/travinov-sv/Documents/DrawioTest/microservices-istio-kafka.drawio"
```

## 11. GigaCode: запустить изолированного Reviewer на DeepSeek

```text
!EXT="$HOME/.gigacode/extensions/publish-drawio-skill"; PROBE="$PWD/.gigacode-probe"; IN="$(find "$PWD/.diagram-runs" -type f -name 'reviewer-audit-input.v2.json' | tail -1)"; echo "INPUT=$IN"; if [ -z "$IN" ]; then echo 'ОШИБКА: reviewer-audit-input.v2.json не найден'; else python3 "$EXT/scripts/agent_runtime.py" reviewer "$IN" --output "$PROBE/reviewer-analysis.v2.json" --cli "$HOME/.gigacode/bin/gigacode" --cwd "$PWD" --timeout 600 > "$PROBE/invocation-result.json" 2> "$PROBE/invocation-error.txt"; RC=$?; echo "exit=$RC"; fi
```

Основная сессия остаётся на MiniMax; дочерний Reviewer должен запуститься
на `vllm/DeepSeek-V4-Flash-262k`.

## Проверка review, trace и перехода к improve без параметров

Сразу после `/drawio:review` из раздела 10 выполните:

```text
/drawio:trace
```

Read-only review записывает `workflow.json`, поэтому trace без `--run`
должен выбрать именно тот же свежий review. В `roles.reviewer` нужны
подтверждённые model/isolation proof, а в v2 evidence — hash-bound
`reviewer-verdict.v2.json`. Модель возвращает только analysis и не задаёт
`receipt_sha256`, provider или итоговые bindings: их формирует Host.

Затем выполните:

```text
/drawio:improve
```

Указывать `--diagram` и `--request` не нужно. В результате ожидаются
`command_resolution.diagram_selection: latest_completed_review`, ссылка на
`command_resolution.review_handoff` и
`command_resolution.request_source: default_review_findings_request`. Если файл был
изменён после review, старый handoff использоваться не должен.

Host отдельно записывает `supervisor_declared_roles`, `host_mandatory_roles` и
их эффективное объединение. Для initial improve в `role_policy.host_mandatory_roles`
ожидаются `repair`, `reviewer`, `semantic_analyst`, `supervisor`. Поэтому ответы
вида `required_roles: [repair, reviewer]` продолжаются через Semantic Analyst и не
завершаются ошибкой `omitted mandatory initial roles`.

## 12. GigaCode: показать фактические модели дочернего процесса

```text
!grep -oE '"model"[[:space:]]*:[[:space:]]*"[^"]+"' "$PWD/.gigacode-probe/runtime-output.jsonl" | sort -u
```

Ожидаемо: `"model": "vllm/DeepSeek-V4-Flash-262k"`.

## 13. GigaCode: показать результат, ошибку и runtime stderr

```text
!echo '=== RESULT ==='; sed -n '1,260p' "$PWD/.gigacode-probe/invocation-result.json"; echo '=== INVOCATION ERROR ==='; sed -n '1,200p' "$PWD/.gigacode-probe/invocation-error.txt"; echo '=== RUNTIME STDERR ==='; sed -n '1,200p' "$PWD/.gigacode-probe/runtime-stderr.txt"
```

При успехе нужны: `resolved_model` и `reported_model` с DeepSeek,
`model_proof.verified: true`, `isolation_proof.verified: true` и `tool_calls: 0`.
Финальный evidence binding появляется в `reviewer-verdict.v2.json`, который
создаёт Host во время `/drawio:review`, а не при отдельной runtime-пробе.

## 14. GigaCode: запустить новую полную мультиагентную цепочку

```text
/drawio:create "Создай тестовую диаграмму обработки заказа с проверкой оплаты, возвратом на исправление при ошибке, комплектацией, доставкой и завершением. Подпиши условия переходов и используй ортогональные соединения с waypoint."
```

Если пользователь отдельно передал готовый roadmap, git-flow или C4 JSON/YAML,
проверьте specialized adapter расширенной формой (путь должен быть внутри
текущего workspace):

```text
/drawio:create --renderer-source "roadmap.json" --request "Построй диаграмму строго по переданному источнику"
```

Ожидаются hash-bound `explicit_user_document`, выбранный specialized adapter и
`fallback: false`. При невалидном документе Host обязан явно записать generic
fallback, а не домыслить данные.

После запуска не вводите `continue`, пока Host сам не вернёт настоящий
checkpoint. В норме Validator и Repair выполняют несколько итераций внутри
одной команды.

## 15. GigaCode: проверить трассировку

```text
/drawio:trace
```

Корректны два исхода: Supervisor завершился на `GigaChat-3-Ultra`; или после
подтверждённого `FatalTurnLimitedError` был ровно один fallback на
`vllm/DeepSeek-V4-Flash-262k`.

Дополнительно проверить MCP-изоляцию последнего запуска:

```text
!RUN="$(ls -td "$PWD"/.diagram-runs/* 2>/dev/null | head -1)"; echo "RUN=$RUN"; grep -n '"allowed_mcp_servers": \[\]' "$RUN/run-manifest.jsonl"; if grep -R -n 'mcp__AtlassianBitbucket\|jira_get_issue' "$RUN"/roles/*/attempts/*/runtime-output.json* 2>/dev/null; then echo 'ОШИБКА: MCP попал в isolated runtime'; else echo 'OK: MCP tool calls отсутствуют'; fi
```

Нужны хотя бы события `role_started`/`role_finished` с `allowed_mcp_servers: []` и
строка `OK: MCP tool calls отсутствуют`. Старый run без checkpoint после ошибки
Supervisor не возобновлять — запускать свежую `/drawio:review` или `/drawio:improve`.

## 16. GigaCode: найти последние артефакты

```text
!find "$PWD/.diagram-runs" -type f \( -name 'host-result.json' -o -name 'run-manifest.jsonl' -o -name 'run-manifest.v2.jsonl' -o -name 'output.json' -o -name 'runtime-output.jsonl' -o -name 'runtime-stderr.txt' -o -name '*.semantic-plan.v2.json' -o -name 'verdict.v2.json' -o -name 'validation-receipt.v2.json' \) -print | tail -60
```

## 17. GigaCode: проверить все фактически вызванные модели

```text
!RUN="$(ls -td "$PWD"/.diagram-runs/* 2>/dev/null | head -1)"; echo "RUN=$RUN"; find "$RUN/roles" -type f -name 'runtime-output.json*' -print0 | while IFS= read -r -d '' F; do echo "===== $F ====="; grep -oE '"model"[[:space:]]*:[[:space:]]*"[^"]+"' "$F" | sort -u; done
```

В полном create/improve-цикле ожидаются роли и модели:

- Supervisor — `GigaChat-3-Ultra` либо один документированный fallback на
  `vllm/DeepSeek-V4-Flash-262k` после `FatalTurnLimitedError`;
- Semantic Analyst — `vllm/Qwen3.6-35B-262k`;
- Repair — `vllm/MiniMax-M3-113k`, только когда есть применимые findings;
- Reviewer — `vllm/DeepSeek-V4-Flash-262k`.

Отсутствие Repair в уже чистом кандидате допустимо. Значение `/stats model`
по-прежнему относится только к основной сессии.

Выход Semantic Analyst должен соответствовать `semantic-analysis.v2`: роль
возвращает complete desired graph, assumptions и human questions, но не SHA,
operation IDs или approval. Canonical `semantic-plan.v2`, точные evidence
bindings и typed delta создаёт Host. В новом run отсутствие legacy-конвертации
проверяется по `workflow.json`: там есть `semantic_analysis_v2` и
`semantic_plan_v2`, а raw output роли имеет `schema_version: 2`.

## 18. GigaCode: проверить автономный цикл и редкий human-in-the-loop

Сначала проверьте, что исправимые попытки продолжались без поддельных
`user_decision` и что raw/host-bound patches сохранены раздельно:

```text
!RUN="$(ls -td "$PWD"/.diagram-runs/* 2>/dev/null | head -1)"; echo "RUN=$RUN"; grep -n 'internal_feedback_created\|auto_retry_scheduled\|user_decision\|patch_bound' "$RUN/run-manifest.jsonl"; find "$RUN/roles" -type f \( -name 'output.json' -o -name 'host-bound.patch.json' \) -print
```

Если `host-result.json` всё же вернул semantic/plateau/final checkpoint,
используйте одну из команд из `next_commands`. Типовой layout-only feedback:

```text
/drawio:resume continue "Сохрани текущую структуру, разведи пересекающиеся стрелки ортогональными waypoint и не удаляй подписанные возвратные петли"
```

Однозначное замечание только про маршруты/waypoint/геометрию должно создать
`repair_scope` и не запускать новую роль `semantic_analyst`. Смешанное замечание
вроде «добавь этап и исправь маршрут» обязано пройти Semantic Analyst и при
семантической дельте запросить подтверждение.

Проверить, что замечание стало новым источником, а decision не применился дважды:

```text
!RUN="$(ls -td "$PWD"/.diagram-runs/* 2>/dev/null | head -1)"; echo "RUN=$RUN"; grep -R -n 'confirmed_clarification\|decision_committed\|semantic_approval' "$RUN/lifecycle-v2" | tail -40
```

Повтор той же команды не должен запускать новую итерацию: ожидается
`already_applied` либо тот же `decision_id`.

## 19. GigaCode: проверить Repair-итерацию на существующей диаграмме

```text
/drawio:improve "Исправь все Validator и Reviewer findings. Сохрани семантику и стабильные ID; прямые соединения через препятствия замени ортогональными маршрутами с различными pins и явными waypoint."
```

Команда должна сама пройти bounded Validator → Repair → Validator цикл. Только
после реального plateau/semantic conflict используйте показанный
`next_commands.continue`. Затем проверьте:

```text
!RUN="$(ls -td "$PWD"/.diagram-runs/* 2>/dev/null | head -1)"; echo "RUN=$RUN"; find "$RUN/roles" -maxdepth 2 -type f \( -name 'input.json' -o -name 'output.json' -o -name 'analysis.v2.json' -o -name 'verdict.v2.json' \) -print; grep -n 'candidate\|validation_receipt\|reviewer_verdict\|checkpoint' "$RUN/lifecycle-v2/run-manifest.v2.jsonl" | tail -40
```

Каждый retry должен начинаться от последнего `working_artifact`. Отклонённый
кандидат остаётся evidence и не становится новой baseline. Первый повторяемый
recoverable failure создаёт `auto_retry_scheduled`; вторая одинаковая
нормализованная сигнатура или общий лимит переводит run в plateau.

## 20. GigaCode: проверить финальное решение и транзакционную публикацию

На `final_acceptance` выберите ровно команду из `next_commands`:

```text
/drawio:resume approve
```

`approve_with_findings` используйте только если эта команда явно предложена
Host. Она допустима только при `strict_passed: true`, Reviewer `approve`,
валидной integrity и отсутствии error findings. Для ручного завершения
используйте:

```text
/drawio:resume manual_handoff "Продолжу редактирование вручную"
```

Проверить publication journal и целевой SHA:

```text
!RUN="$(ls -td "$PWD"/.diagram-runs/* 2>/dev/null | head -1)"; echo "RUN=$RUN"; find "$RUN/lifecycle-v2/snapshots/publication-transaction" -type f -maxdepth 1 -print -exec sed -n '1,240p' {} \; 2>/dev/null; sed -n '1,260p' "$RUN/host-result.json"
```

Create обязан использовать no-clobber, improve — compare-and-swap с rollback
copy. Если целевой файл изменился после начала run, ожидается
`publication_conflict`, а не перезапись.

## 21. GigaCode: финальная read-only проверка trace

```text
/drawio:trace
```

Для нового run нужны валидные `control_plane_v2`, event/snapshot bindings,
accepted artifact/receipt и publication transaction. Trace не должен создавать,
восстанавливать или менять файлы. Для старого v1 run он остаётся read-only и
предлагает manual handoff; mutable resume v1 запрещён.

Runtime не сканирует проект в поиске OpenSpec. Только документ, явно переданный
пользователем, входит в source bundle как `explicit_user_document`.

## 22. Terminal: откат на сохранённую `1.23.0-corporate.13`

Сначала остановите GigaCode, затем в распакованном архиве новой версии:

```bash
cd "$HOME/Downloads/drawio-skill" && bash install/rollback_drawio_agent_extension.sh --latest && sed -n '1,20p' "$HOME/.gigacode/extensions/publish-drawio-skill/gemini-extension.json" && "$HOME/.gigacode/bin/gigacode" extensions list
```

Ожидается восстановленная версия `1.23.0-corporate.13` и зарегистрированный
`publish-drawio-skill`. Verifier из распакованного `1.24` намеренно не запускайте
против старой версии: он проверяет контракт своей поставки. Не удаляйте backup
вручную до проверки.

## 23. Файлы для передачи на анализ

Из `.gigacode-probe`:

- `invocation-result.json`;
- `invocation-error.txt`;
- `runtime-output.jsonl`;
- `runtime-stderr.txt`.

Из нового `.diagram-runs/<run-id>`:

- `host-result.json`;
- `run-manifest.jsonl`;
- `lifecycle-v2/run-manifest.v2.jsonl`;
- последние snapshots из `lifecycle-v2/snapshots/`;
- `workflow.json`;
- raw `roles/semantic-initial/output.json` и canonical файл из
  `semantic-plans/*.semantic-plan.v2.json`;
- `roles/supervisor-initial/attempts/primary/runtime-output.jsonl`;
- `roles/supervisor-initial/attempts/fallback-1/runtime-output.jsonl`, если fallback был вызван.

`/stats model` показывает модель основной сессии и не является доказательством
модели дочернего процесса. Доказательство — поля `system.model` и
`assistant.message.model` в `runtime-output.jsonl`.
