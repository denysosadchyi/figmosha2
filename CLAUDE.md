# Figmosha — інструкції для Claude Code

## Що це

Малюємо дизайн у Figma через Scripter плагін (Figma Plugin API).
Правила генерації коду — в `scripter.md` (скіл figma-scripter).

## run.py — основний спосіб

Окремий Python-процес тримає Firefox з Figma і Scripter.

### Запуск сервера (раз, в окремому терміналі)

```bash
DISPLAY=:99 python -u run.py --serve FIGMA_URL EMAIL PASSWORD
```

### Виконання коду

```bash
python run.py "figma plugin api код"
```

Результат: `result.png` (якщо потрібен). Два tool calls: Bash + Read.

### Без скріншотів

Не робити скріншот якщо користувач не просить побачити результат.
`figma.notify()` в try/catch підтвердить успіх. Сервер виводить "OK".

## MCP fallback

Якщо run.py недоступний — один `browser_run_code` з clipboard paste + Run.

Firefox: `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`
Профіль зайнятий: `pkill -f "firefox.*mcp-firefox"`

## Браузер

Playwright MCP з Firefox. Конфіг у `.mcp.json`.
Xvfb віртуальний дисплей (`DISPLAY=:99`) для headed режиму.
