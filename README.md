# Figmosha

AI-автоматизація дизайну у Figma. Claude Code малює UI-компоненти у Figma, виконуючи скрипти Plugin API через браузерну автоматизацію (Playwright + плагін Scripter).

## Як це працює

```
Claude Code  →  run.py (Playwright/Firefox)  →  Figma Scripter  →  Figma canvas
```

1. `run.py` запускає Firefox, логіниться у Figma і відкриває плагін Scripter
2. Claude Code надсилає скрипти Figma Plugin API через named pipe (`/tmp/figmosha.fifo`)
3. Scripter виконує код всередині Figma — створює фрейми, компоненти, текст, auto layout
4. Стан канвасу зчитується через `output.txt` дампи (без скріншотів)

## Встановлення

### Вимоги

- Python 3.10+
- Playwright (`pip install playwright && playwright install firefox`)
- Xvfb для headless Linux (`sudo apt install xvfb`)
- Акаунт Figma з встановленим плагіном [Scripter](https://www.figma.com/community/plugin/757836922707087381)

### Linux (AppArmor фікс)

```bash
sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
```

### Віртуальний дисплей

```bash
Xvfb :99 -screen 0 1920x1080x24 &
```

### Запуск сервера

```bash
DISPLAY=:99 python -u run.py --serve FIGMA_FILE_URL EMAIL PASSWORD
```

### Виконання коду

```bash
# Inline
python run.py "figma.createRectangle()"

# З файлу
python run.py --file script.js
```

### Плагіни та Propstar

```bash
# Запустити плагін (наприклад Propstar)
python run.py "__plugin__:Propstar > Create property table"

# Перевідкрити Scripter після плагіна
python run.py "__reopen_scripter__"
```

## Структура проєкту

```
run.py              — Playwright сервер: автоматизація браузера + fifo listener
scripter.md         — Правила генерації коду для Figma Scripter
add-component.md    — Універсальний пайплайн додавання компонентів з коду в Figma
pdf-import.md       — Пайплайн імпорту PDF презентацій в Figma
figma-comments.md   — Читання коментарів з Figma через REST API та виконання правок
CLAUDE.md           — Інструкції для сесій Claude Code
plugin/             — Кастомний Figma плагін (альтернатива Scripter)
  code.js           — Бекенд плагіна (eval + print)
  ui.html           — UI плагіна (редактор коду + вивід)
  manifest.json     — Маніфест плагіна
```

## Ключові концепти

- **Два етапи**: Створення візуальної структури (Step 1) окремо від прив'язки змінних (Step 2) — змішування в одному скрипті призводить до мовчазних збоїв
- **Без скріншотів**: Стан канвасу перевіряється через `print()` дампи в Scripter, а не скріншоти — економить токени і надійніше
- **Атомарний підхід**: Складні компоненти збираються з інстансів атомарних компонентів. Стилі прив'язуються лише на атомах — інстанси наслідують автоматично
- **Figma Variables**: Всі кольори, радіуси, розміри прив'язуються до Figma Variables через `setBoundVariableForPaint()` (fills/strokes) та `setBoundVariable()` (числові)
- **Text Styles**: Всі тексти отримують локальні Figma Text Styles (body/sm/medium, heading/h1/bold тощо)
- **Propstar**: Після створення Component Set обов'язково запускається Propstar для розкладки варіантів у сітку
- **Clipboard paste**: Код інжектиться через clipboard (`navigator.clipboard.writeText` + Ctrl+V)

## Правила Scripter

Див. [`scripter.md`](scripter.md) — повний набір правил для запобігання runtime помилкам:

- Завантажити шрифти перед текстовими операціями
- `appendChild()` перед `resize()` або layout властивостями
- `layoutMode` перед будь-якими auto layout пропсами
- Кольори в RGB 0-1, не hex
- `findOne()` для текстових overrides в інстансах

## Робота з коментарями Figma

Див. [`figma-comments.md`](figma-comments.md) — читання коментарів через REST API:

```
Fetch unresolved comments → Parse → Apply via Scripter → Verify
```

Потрібен **Figma Personal Access Token** (генерується на https://www.figma.com/developers/api#access-tokens).

## Імпорт PDF презентацій

Див. [`pdf-import.md`](pdf-import.md) — пайплайн імпорту PDF в Figma:

```
Прочитати PDF → Аналіз слайдів → 1 скрипт на слайд → Верифікація
```

Тексти переносяться повністю, зображення та графіки замінюються на placeholder-прямокутники. Кожен слайд — окремий фрейм 1920x1080.

## Пайплайн додавання компонентів

Див. [`add-component.md`](add-component.md) — універсальний пайплайн:

```
Прочитати код → Step 1 (створити) → Перевірити розміри →
Step 2 (прив'язати змінні) → Перевірити прив'язки → Propstar
```

Включає маппінг кольорів, радіусів, текстових стилів, хелпери `bF()`, `bS()`, `bN()`, `bT()`, `bR()`, `bE()`, таблицю типових помилок.

## Ліцензія

MIT
