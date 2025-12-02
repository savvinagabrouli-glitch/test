# send_messages CLI

CLI-утилита для автоматизации отправки сообщений в диалогах leboncoin.fr через официальный HTTP API.

## Установка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Запуск

1. Скопируйте `config.json` и обновите токены аккаунтов, ссылки на диалоги и переменные для шаблона.
2. Выполните dry-run (ничего не отправляется, но учитывается идемпотентность):

```bash
python send_messages.py --config config.json --dry-run
```

3. Для реальной отправки уберите флаг `--dry-run`:

```bash
python send_messages.py --config config.json
```

После выполнения будут сформированы:
- `logs.jsonl` — журнал всех попыток (timestamp, account, recipient, status, error).
- `.send_messages_state.json` — файл идемпотентности, предотвращает повторную отправку одинаковых сообщений одному получателю.
