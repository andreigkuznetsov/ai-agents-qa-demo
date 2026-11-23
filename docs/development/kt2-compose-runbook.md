# КТ2. Запуск и приёмка Compose

Запускайте команды из корня чистого checkout. Для приёмки нужен полный SHA
кандидата из PR. Используйте Python 3.12 или новее, Git, Docker Engine с
Linux-контейнерами и плагин Docker Compose с поддержкой `--wait`.
На Windows это Docker Desktop в режиме Linux containers. Запустите его вручную
и дождитесь доступности Engine. В приёмке PR запуск выполняется после успешного
Linux CI и подтверждения готовности Desktop владельцем компьютера.

Драйвер сначала проверяет SHA, чистоту checkout, локальный Docker endpoint и
версию Engine. Он отклоняет переменные `DOCKER_HOST`,
`DOCKER_CONFIG`, `COMPOSE_*`, `QA_*` и
`DOCKER_CONTEXT` с другим контекстом. Используйте отдельную оболочку
без настроек другого проекта. Контекст указывается явно. Команда
`docker context ls` позволяет посмотреть имена контекстов.

## Linux

В локальной оболочке подставьте **полный SHA принятого на проверку кандидата**.
Контекст `default` должен указывать на локальный Unix socket. Для rootless
Engine укажите имя локального контекста явно.

```sh
candidate=<полный-SHA-кандидата>
python3 scripts/compose-acceptance.py --linux --context default \
  --expected-sha "$candidate" \
  --report-dir "local-runs/kt2-pr3/linux-$(date +%Y%m%d-%H%M%S)"
```

В GitHub Actions выполняется тот же драйвер из чистого checkout с
`--linux-ci`. На выделенном runner он дополнительно проверяет потерю
PostgreSQL, восстановление readiness и отказ миграции в отдельном новом
Compose-проекте. Локальный режим запускает позитивные сценарии.

## Windows

После подтверждения готовности Docker Desktop внесите полный SHA кандидата и
выполните команды в PowerShell в VS Code. Для Linux-контейнеров Docker Desktop
обычно используется `desktop-linux`. Если имя другое, сверяйте его
с `docker context ls` и укажите фактический локальный контекст Desktop.

```powershell
$candidate = '<полный-SHA-кандидата>'
$report = 'local-runs/kt2-pr3/windows-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
py -3 scripts/compose-acceptance.py --windows --context desktop-linux --expected-sha $candidate --report-dir $report
py -3 scripts/compose-acceptance.py --expected-sha $candidate --verify-report "$report/report.json"
```

Каждый прогон использует новый каталог отчётов. Windows-отчёт первоначально
хранится в `local-runs/kt2-pr3/` вне Git. Перед публикацией проверьте
SHA, платформу, версии, результаты и отсутствие секретов. Очищенный отчёт
прикладывается к [Issue №3](https://github.com/andreigkuznetsov/ai-agents-qa-demo/issues/3)
или хранится в доступном артефакте со ссылкой из Issue.

## Что происходит в тестовом проекте

Драйвер создаёт проект `kt2caf-…-positive`, сервисы `db`,
`migrate`, `api`, сеть `<project>_default`, том
`<project>_postgres` и образ `ai-agents-qa-demo:kt2caf-…`.
HTTP использует выделенный Docker loopback-порт. PostgreSQL доступна только
внутри сети Compose. Одноразовые пароли записываются во временный env-file
вне checkout. В БД создаётся одна синтетическая запись из четырёх полей
через RunStore. После штатной остановки драйвер повторно запускает стек
с тем же томом и сверяет запись. На Linux CI для отказа миграции создаётся
отдельный проект `kt2caf-…-negative` с отдельным томом.

Штатная остановка сохраняет контейнеры, том и данные тестового проекта.
При сомнении в принадлежности ресурса драйвер прекращает операции с ним.
Удаление тома уничтожает данные и требует отдельного согласования. При
прерванном запуске используйте имя проекта из отчёта и проверяйте Compose
labels ресурсов в том же контексте перед любыми ручными действиями.

## Диагностика

`report.json` содержит факты проверок, а `summary.md`
показывает этапы, длительность и безопасную категорию ошибки. Например,
`migration-failure` указывает на несовпадение ожидаемого результата
проверки миграции, а `ownership` требует сверки идентификаторов
и labels. При недоступной БД ожидаются `200` от liveness и
`503` от readiness-маршрутов. После возврата БД readiness должен
вернуться к `200` в пределах окна восстановления. Сырые журналы,
credentials и DSN не входят в отчёты.

Отсутствие фактического Windows-прогона означает **NOT VERIFIED** и оставляет
полную приёмку КТ2 открытой. Начатый неуспешный прогон имеет статус **FAIL**.
Linux Actions сохраняет очищенные отчёты 30 дней. Отсутствующий отчёт или
отчёт на другом SHA не подтверждает приёмку кандидата.
