# obrabot4

Минимальный публичный транспорт для обращений закрытого приложения к OpenAI через GitHub-hosted Actions.

Репозиторий содержит только:

- ограниченный JSONL-протокол;
- HTTP-клиент OpenAI Responses API с обязательным `store: false`;
- SSH-клиент с проверкой входных параметров;
- ручные GitHub Actions workflows для обработки и проверенного деплоя.

Здесь нет исходного кода панели, базы профилей, бизнес-правил, пользовательских данных и секретов. Закрытое приложение формирует запрос, проверяет структурированный ответ и сохраняет результат на собственном сервере.

## Необходимые GitHub Secrets

- `OPENAI_API_KEY`
- `REG_RU_HOST`
- `REG_RU_USER`
- `REG_RU_SSH_KEY`
- `REG_RU_KNOWN_HOSTS`
- `LUMA_READ_SSH_KEY` — отдельный read-only deploy key закрытого репозитория.

SSH-ключ должен быть отдельным и ограниченным forced command на сервере. Workflow запускается только вручную из ветки `main`; события `pull_request` и расписание не используют секреты.

## Проверка

```bash
python -m pip install -e '.[test]'
python -m pytest -q -W error
bash -n scripts/run-profile-audit-worker.sh
```
