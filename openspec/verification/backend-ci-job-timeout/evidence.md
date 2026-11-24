# Проверка лимита Backend CI

Исходная `main` — `53268770bca5b4627c3349522aabf70cd14d44e8`. Проверенный HEAD коммита с изменением workflow — `15a371b40ed5d9f75d56e1aa0b73559d94674883`. Команда принимает итоговый HEAD PR вторым аргументом после пути к парсеру, чтобы повторить проверку на коммите с манифестом.

Command: Из корня репозитория выполнить сравнение Git blob и разбор YAML. Использован Node.js 22.12.0 и уже установленный js-yaml 3.14.1.

```powershell
$yaml = (Resolve-Path "$env:USERPROFILE/.vscode/extensions/ritwickdey.liveserver-5.7.10/node_modules/js-yaml").Path
@'
const {execFileSync} = require("node:child_process");
const assert = require("node:assert/strict");
const yamlPath = process.argv[2];
const yaml = require(yamlPath);
const {version} = require(yamlPath + "/package.json");
const path = ".github/workflows/ci.yml";
const read = ref => execFileSync("git", ["show", ref + ":" + path]);
const before = read("53268770bca5b4627c3349522aabf70cd14d44e8");
const after = read(process.argv[3] || "15a371b40ed5d9f75d56e1aa0b73559d94674883");
const oldLine = Buffer.from("    timeout-minutes: 10\n");
const newLine = Buffer.from("    timeout-minutes: 15\n");
const at = before.indexOf(oldLine);
assert.ok(at >= 0 && before.indexOf(oldLine, at + 1) === -1);
assert.deepEqual(after, Buffer.concat([
  before.subarray(0, at), newLine, before.subarray(at + oldLine.length)
]));
const parse = bytes => yaml.safeLoad(bytes.toString("utf8"));
const base = parse(before);
const head = parse(after);
assert.deepEqual(Object.keys(head.jobs), ["backend"]);
assert.equal(base.jobs.backend["timeout-minutes"], 10);
assert.equal(head.jobs.backend["timeout-minutes"], 15);
base.jobs.backend["timeout-minutes"] = 15;
assert.deepEqual(head, base);
console.log("PASS: workflow bytes and structure match; backend timeout 10 -> 15; js-yaml " + version);
'@ | node - $yaml
```

Result: PASS

Evidence: Команда завершилась с кодом 0 и вывела `PASS: workflow bytes and structure match; backend timeout 10 -> 15; js-yaml 3.14.1`. [Прогоны Backend CI для ветки технического PR](https://github.com/andreigkuznetsov/ai-agents-qa-demo/actions/workflows/ci.yml?query=branch%3Atooling%2Fbackend-ci-job-timeout).

Сравнение подтвердило единственную замену строки и целое значение `jobs.backend.timeout-minutes = 15`. Структуры YAML совпали после этой замены. Остальные байты workflow, команды, условия, зависимости, тестовый набор и отдельные тайм-ауты сохранены. PASS выше относится к локальной проверке конфигурации. Результаты обязательных CI и ревью на полном HEAD фиксируются в PR.
